#!/usr/bin/env python3
"""The cheaper seat, logged and told: every seat event a Claude Code hook can see, a notification for the cheaper ones,
and a week's tally.

A seat is a model at a reasoning effort. William's house rule of 2026-10-27 says a session on the dearest seat (Claude
Fable 5.1 at max) hands bounded, checkable work to a subagent on a cheaper one and suggests a cheaper seat for its own
session, leaving that change to him. William, 2026-10-27: "Is there a way for us to get notified if model or effort is
reduced (some way we can log that it's working). That way we know it's working and we can push it to github as another
skill". So:

  delegate     PreToolUse on Agent: the session's seat (its transcript's last real assistant entry) against the
               child's (tool_input.model, or the parent's model by default; tool_input.effort; a fork keeps the
               parent's seat). Told and notified when the child is cheaper.
  set_seat     PreToolUse on set_session_model or set_session_effort: another session's seat moved by a tool call.
               Always told and notified, since a session never moves a seat unasked.
  seat_change  Stop: the turn's seat against the one saved at the previous Stop. Told for any change, notified when
               cheaper. The first Stop of a session only saves.
  suggest      Stop: a cheaper-seat suggestion in the turn's text or its wrap_up (a heuristic, SUGGEST below), each
               sentence logged once per session. Neither told nor notified.

The hook only logs: it never blocks a tool, prints nothing or one {"systemMessage"} object, and on any error writes one
line to errors.log and exits 0. Standard library only, so the file stands alone.

  python seat_log.py hook                                     the hook (PreToolUse or Stop JSON on stdin)
  python seat_log.py report [--days N | --since YYYY-MM-DD] [--json]   the tally, default the last 7 days
  python seat_log.py selftest

Knobs (environment variables of the same name override each):
  SEAT_LOG_DIR       ~/.claude/seat-log: events.jsonl, state/<session>.json, errors.log
  SEAT_LOG_PROJECTS  ~/.claude/projects: the transcripts the report reads
  SEAT_LOG_APPDIR    ~/AppData/Local/Claude-Data/*/claude-code-sessions: the desktop app's session records, read on
                     Windows for titles (a glob, best effort)
  SEAT_LOG_TOAST     1; 0 silences the notifications
"""
from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

SEAT_LOG_DIR = "~/.claude/seat-log"
SEAT_LOG_PROJECTS = "~/.claude/projects"
SEAT_LOG_APPDIR = "~/AppData/Local/Claude-Data/*/claude-code-sessions"
SEAT_LOG_TOAST = "1"

TAIL = 256 * 1024                    # the transcript's tail read first
TAIL_MAX = 8 * 1024 * 1024           # the most a hook reads of a transcript: a turn that starts further back is read from here
STDIN_CAP = 4 * 1024 * 1024
KEEP_HASHES = 20
MODEL_RANKS = (("haiku", 1), ("sonnet", 2), ("opus", 3), ("fable", 4), ("mythos", 4))
EFFORT_RANKS = {"low": 1, "medium": 2, "high": 3, "xhigh": 4, "max": 5}
SET_TOOLS = {"mcp__ccd_session_mgmt__set_session_model": "model", "mcp__ccd_session_mgmt__set_session_effort": "effort"}
WRAP_UP = "mcp__ccd_turn__wrap_up"
SUGGEST = re.compile("|".join([
    r"\b(?:a|the|this|to) (?:cheaper|lower) (?:seat|effort)\b",
    r"\bat a lower effort\b",
    r"\blower (?:the |its |my )?effort\b",
    r"\bsubagent on (?:opus|sonnet|haiku)\b",
    r"\b(?:opus|sonnet|haiku) (?:subagent|at (?:low|medium|high)\b)",
    r"\bdelegat\w* to (?:opus|sonnet|haiku)\b",
]), re.I)

Seat = tuple  # (model, effort); either may be None
Show = Callable[[str, str], tuple]


def knob(name: str) -> str:
    return os.environ.get(name) or globals()[name]


def knob_path(name: str) -> Path:
    v = knob(name)
    return Path.home() / v[2:] if v.startswith("~/") else Path(v)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- seats ----------

def model_rank(model: str | None) -> int | None:
    m = (model or "").lower()
    return next((r for k, r in MODEL_RANKS if k in m), None)


def effort_rank(effort: str | None) -> int | None:
    return EFFORT_RANKS.get((effort or "").lower())


def direction(child: Seat, parent: Seat) -> str:
    cm, pm = model_rank(child[0]), model_rank(parent[0])
    if cm is None or pm is None:
        return "unknown"
    ce, pe = effort_rank(child[1]), effort_rank(parent[1])
    if ce is None or pe is None:
        return "cheaper" if cm < pm else "dearer" if cm > pm else "same"
    if cm == pm and ce == pe:
        return "same"
    if (cm < pm and ce <= pe) or (cm == pm and ce < pe):
        return "cheaper"
    if (cm > pm and ce >= pe) or (cm == pm and ce > pe):
        return "dearer"
    return "mixed"


def seat_text(seat: Seat) -> str:
    return f"{seat[0] or 'an unknown model'} at {seat[1] or 'its default effort'}"


def seat_obj(seat: Seat) -> dict:
    return {"model": seat[0], "effort": seat[1]}


# ---------- transcripts ----------

def entry_seat(e: dict) -> Seat | None:
    """The seat of a real assistant entry, or None (another kind of entry, or a synthetic one)."""
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    if e.get("type") != "assistant" or "<synthetic>" in (e.get("requestedModel"), msg.get("model")):
        return None
    model = e.get("requestedModel") or msg.get("model")
    return (model, e.get("effort") or e.get("perTurnEffort")) if model else None


def is_human(e: dict) -> bool:
    if e.get("type") != "user" or e.get("isMeta"):
        return False
    c = (e.get("message") or {}).get("content")
    return isinstance(c, str) or (isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "text" for b in c))


def scan(data: bytes, need_turn: bool) -> tuple[Seat | None, list[dict], bool]:
    """From the end of data: the last real seat, the assistant entries after the last human message (oldest first),
    and whether that human message was found."""
    seat, turn, closed = None, [], False
    for line in reversed(data.splitlines()):
        if b'"type":"assistant"' not in line and not (need_turn and b'"type":"user"' in line):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if need_turn and not closed:
            if is_human(e):
                closed = True
            elif e.get("type") == "assistant":
                turn.append(e)
        seat = seat or entry_seat(e)
        if seat and (closed or not need_turn):
            break
    return seat, turn[::-1], closed


def read_transcript(path: Path, need_turn: bool = False) -> tuple[Seat | None, list[dict]]:
    """The last 256 KB first (from a whole line), then the last 8 MB when that holds no seat or, at a Stop, not the
    turn's start; never the whole file, since the hook runs on every turn of every session."""
    size = path.stat().st_size
    seat, turn = None, []
    for limit in (TAIL, TAIL_MAX):
        with open(path, "rb") as f:
            f.seek(max(0, size - limit))
            data = f.read()
        if size > limit:
            data = data[data.find(b"\n") + 1:]
        seat, turn, closed = scan(data, need_turn)
        if size <= limit or (seat and (closed or not need_turn)):
            break
    return seat, turn


def blocks(e: dict) -> list[dict]:
    c = (e.get("message") or {}).get("content")
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def sentence(text: str, a: int, b: int) -> str:
    start = max(text.rfind(c, 0, a) for c in ".!?\n") + 1
    ends = [i for i in (text.find(c, b) for c in ".!?\n") if i != -1]
    return " ".join(text[start:min(ends) + 1 if ends else len(text)].split())[:240]


def suggestions(turn: list[dict]) -> list[tuple[str, str]]:
    """(where, sentence) for each cheaper-seat suggestion in the turn's text blocks and wrap_up fields."""
    found = []
    for e in turn:
        for b in blocks(e):
            if b.get("type") == "text":
                texts = [("text", b.get("text"))]
            elif b.get("type") == "tool_use" and b.get("name") == WRAP_UP and isinstance(b.get("input"), dict):
                inp = b["input"]
                acts = [a for a in inp.get("actions") or [] if isinstance(a, dict)]
                texts = [("wrap_up", t) for t in [inp.get("recap"), inp.get("ask")]
                         + [a.get(k) for a in acts for k in ("label", "message")]]
            else:
                continue
            for where, t in texts:
                if isinstance(t, str):
                    found += [(where, sentence(t, m.start(), m.end())) for m in SUGGEST.finditer(t)]
    return found


# ---------- the log ----------

@contextmanager
def locked(path: Path, tries: int = 50):
    """The operating system's lock on one byte of path: it dies with the process holding it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        if os.name == "nt":
            import msvcrt
            for _ in range(tries):
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError(f"{path} is held by another run")
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def append_events(events: list[dict]) -> None:
    d = knob_path("SEAT_LOG_DIR")
    with locked(d / "events.jsonl.lock"):
        with open(d / "events.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.writelines(json.dumps(ev) + "\n" for ev in events)


def state_path(session: str) -> Path:
    return knob_path("SEAT_LOG_DIR") / "state" / (re.sub(r"[^A-Za-z0-9_-]", "_", session) + ".json")


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


def log_error(e: BaseException) -> None:
    try:
        d = knob_path("SEAT_LOG_DIR")
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "errors.log", "a", encoding="utf-8", newline="\n") as f:
            f.write(f"{now_iso()} {type(e).__name__}: {str(e)[:300]}\n")
    except OSError:
        pass


# ---------- the notification ----------

def toast_script(title: str, text: str) -> str:
    xml = ('<toast><visual><binding template="ToastGeneric">'
           f"<text>{escape(title)}</text><text>{escape(text)}</text></binding></visual></toast>")
    literal = "'" + xml.replace("'", "''") + "'"
    app = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null",
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null",
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument",
        f"$x.LoadXml({literal})",
        f"$n = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app}')",
        "if ($n.Setting -ne 'Enabled') { 'not shown: ' + $n.Setting; exit 3 }",
        "$n.Show([Windows.UI.Notifications.ToastNotification]::new($x))",
        "'shown'",
    ])


def show_notification(title: str, text: str) -> tuple[bool, str]:
    """A notification on Windows or macOS: (ok, why). Never raises."""
    try:
        if os.name == "nt":
            enc = base64.b64encode(toast_script(title, text).encode("utf-16-le")).decode("ascii")
            cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc]
        elif sys.platform == "darwin":
            q = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            cmd = ["osascript", "-e", f'display notification "{q(text)}" with title "{q(title)}"']
        else:
            return False, "no notifier on this platform"
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
    except (OSError, subprocess.SubprocessError) as e:
        return False, type(e).__name__
    out = (r.stdout or "").strip().splitlines()
    if r.returncode == 0 and (os.name != "nt" or (out and out[-1] == "shown")):
        return True, ""
    return False, out[-1] if out else f"exit {r.returncode}"


def notify(note: str, show: Show | None) -> tuple[bool, str]:
    if knob("SEAT_LOG_TOAST") == "0":
        return False, "silenced"
    return (show or show_notification)("Seat log", note.removeprefix("Seat log: "))


# ---------- the hook ----------

def on_pre_tool(data: dict, base: dict) -> tuple[str | None, bool]:
    tool, inp = data.get("tool_name"), data.get("tool_input") or {}
    if tool in SET_TOOLS:
        key = SET_TOOLS[tool]
        append_events([{**base, "event": "set_seat", "target": inp.get("session_id"), key: inp.get(key)}])
        return f"Seat log: this session set {inp.get('session_id')}'s {key} to {inp.get(key)} by tool", True
    if tool != "Agent":
        return None, False
    parent = read_transcript(Path(data["transcript_path"]))[0] or (None, None)
    fork = inp.get("subagent_type") == "fork"
    child = parent if fork else (inp.get("model") or parent[0], inp.get("effort"))
    d = "same" if fork else direction(child, parent)
    desc = str(inp.get("description") or "")[:200]
    append_events([{**base, "event": "delegate", "parent": seat_obj(parent),
                    "child": {**seat_obj(child), "type": inp.get("subagent_type"), "fork": fork},
                    "direction": d, "description": desc}])
    if d != "cheaper":
        return None, False
    return f"Seat log: delegated to {seat_text(child)} (this session: {seat_text(parent)}): {desc}", True


def on_stop(data: dict, base: dict) -> tuple[str | None, bool]:
    seat, turn = read_transcript(Path(data["transcript_path"]), need_turn=True)
    path = state_path(base["session"])
    state = load_state(path)
    old = tuple(state["seat"]) if state.get("seat") else None
    events, note, toast = [], None, False
    if seat and old and seat != old:
        d = direction(seat, old)
        events.append({**base, "event": "seat_change", "from": seat_obj(old), "to": seat_obj(seat), "direction": d})
        note, toast = f"Seat log: this session moved from {seat_text(old)} to {seat_text(seat)}", d == "cheaper"
    hashes = list(state.get("hashes") or [])
    for where, text in suggestions(turn):
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if h not in hashes:
            hashes = (hashes + [h])[-KEEP_HASHES:]
            events.append({**base, "event": "suggest", "where": where, "text": text})
    if events:
        append_events(events)
    save_state(path, {"seat": list(seat or old or []) or None, "hashes": hashes})
    return note, toast


def hook(raw: str, show: Show | None = None) -> str | None:
    """Log what the hook input shows; the systemMessage, or None."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("hook input is not a JSON object")
    event = data.get("hook_event_name") or ("PreToolUse" if data.get("tool_name") else "")
    base = {"ts": now_iso(), "event": "", "session": data.get("session_id") or "", "cwd": data.get("cwd") or ""}
    handler = {"PreToolUse": on_pre_tool, "Stop": on_stop}.get(event)
    if not handler:
        return None
    note, toast = handler(data, base)
    if note and toast:
        notify(note, show)
    return note


def run_hook(show: Show | None = None) -> int:
    try:
        note = hook(sys.stdin.buffer.read(STDIN_CAP).decode("utf-8", "replace"), show)
        if note:
            print(json.dumps({"systemMessage": note}))
    except Exception as e:
        log_error(e)
    return 0


# ---------- the report ----------

def titles() -> dict:
    """The desktop app's title for each transcript uuid (Windows, best effort)."""
    out = {}
    if os.name != "nt":
        return out
    for root in glob.glob(str(knob_path("SEAT_LOG_APPDIR"))):
        for p in Path(root).rglob("local_*.json"):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
                if r.get("cliSessionId") and r.get("title"):
                    out[r["cliSessionId"]] = r["title"]
            except (OSError, ValueError, AttributeError):
                continue
    return out


def gather(since: datetime) -> dict:
    cut = since.strftime("%Y-%m-%dT%H:%M:%S")
    turns, seen, sessions = {}, set(), {}
    agents = {"total": 0, "with_model": 0, "with_effort": 0}
    for p in knob_path("SEAT_LOG_PROJECTS").rglob("*.jsonl"):
        try:
            if p.stat().st_mtime < since.timestamp():
                continue
            lines = p.read_bytes().splitlines()
        except OSError:
            continue
        for line in lines:
            if b'"type":"assistant"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            seat = entry_seat(e) if isinstance(e, dict) else None
            if not seat or str(e.get("timestamp") or "") < cut:
                continue
            row = turns.setdefault(seat, {"model": seat[0], "effort": seat[1], "entries": 0, "responses": 0,
                                          "input_tokens": 0, "cache_creation_input_tokens": 0,
                                          "cache_read_input_tokens": 0, "output_tokens": 0})
            row["entries"] += 1
            msg = e.get("message") or {}
            if msg.get("id") not in seen:          # each entry of one response repeats its usage
                seen.add(msg.get("id"))
                row["responses"] += 1
                for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
                    row[k] += int((msg.get("usage") or {}).get(k) or 0)
            for b in blocks(e):
                if b.get("type") == "tool_use" and b.get("name") == "Agent" and isinstance(b.get("input"), dict):
                    agents["total"] += 1
                    agents["with_model"] += bool(b["input"].get("model"))
                    agents["with_effort"] += bool(b["input"].get("effort"))
            if not e.get("isSidechain"):
                sessions.setdefault(e.get("sessionId") or p.stem, set()).add(seat)
    events = []
    try:
        for line in (knob_path("SEAT_LOG_DIR") / "events.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and str(ev.get("ts") or "") >= cut:
                events.append(ev)
    except OSError:
        pass
    names = titles()
    for ev in events:
        ev["title"] = names.get(ev.get("session"))
    by_dir = {}
    for ev in events:
        if ev.get("event") == "delegate":
            by_dir[ev.get("direction")] = by_dir.get(ev.get("direction"), 0) + 1
    return {
        "since": cut + "Z",
        "turns": sorted(turns.values(), key=lambda r: -r["entries"]),
        "delegations": {"by_direction": by_dir, "agent_calls": agents,
                        "cheaper": [ev for ev in events if ev.get("event") == "delegate" and ev.get("direction") == "cheaper"]},
        "moves": [ev for ev in events if ev.get("event") in ("seat_change", "set_seat")],
        "suggestions": [ev for ev in events if ev.get("event") == "suggest"],
        "multi_seat": [{"session": s, "title": names.get(s), "seats": sorted([list(x) for x in v], key=str)}
                       for s, v in sorted(sessions.items()) if len(v) > 1],
    }


def who(ev: dict) -> str:
    s = str(ev.get("session") or "")[:8]
    return f"{s} {ev['title']!r}" if ev.get("title") else s


def render(r: dict) -> str:
    seat = lambda o: seat_text((o.get("model"), o.get("effort")))
    out = [f"Seat log since {r['since']}", "", "Turns by seat"]
    out += [f"  {seat(t)}: {t['entries']} entries, {t['responses']} responses, input {t['input_tokens']}, cache write "
            f"{t['cache_creation_input_tokens']}, cache read {t['cache_read_input_tokens']}, output {t['output_tokens']}"
            for t in r["turns"]] or ["  (none)"]
    d = r["delegations"]
    out += ["", "Delegations",
            "  " + (", ".join(f"{k} {v}" for k, v in sorted(d["by_direction"].items())) or "no delegate events")]
    out += [f"  {ev['ts']} {who(ev)}: {seat(ev['parent'])} to {seat(ev['child'])}: {ev.get('description')}"
            for ev in d["cheaper"]]
    a = d["agent_calls"]
    out.append(f"  Agent calls in the transcripts: {a['total']}, {a['with_model']} with a model, "
               f"{a['with_effort']} with an effort")
    out += ["", "Seat moves"]
    for ev in r["moves"]:
        if ev["event"] == "seat_change":
            out.append(f"  {ev['ts']} {who(ev)}: moved from {seat(ev['from'])} to {seat(ev['to'])} ({ev['direction']})")
        else:
            key = "model" if "model" in ev else "effort"
            out.append(f"  {ev['ts']} {who(ev)}: set {ev.get('target')}'s {key} to {ev.get(key)} by tool")
    out += [] if r["moves"] else ["  (none)"]
    out += ["", "Suggestions (heuristic)"]
    out += [f"  {ev['ts']} {who(ev)} [{ev.get('where')}]: {ev.get('text')}" for ev in r["suggestions"]] or ["  (none)"]
    out += ["", "Sessions on more than one seat in the window"]
    out += [f"  {who(m)}: " + ", ".join(seat_text(tuple(s)) for s in m["seats"]) for m in r["multi_seat"]] or ["  (none)"]
    return "\n".join(out)


def report(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="seat_log.py report")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--days", type=float, default=7)
    g.add_argument("--since", help="YYYY-MM-DD, from local midnight")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    since = (datetime.strptime(a.since, "%Y-%m-%d").astimezone(timezone.utc) if a.since
             else datetime.now(timezone.utc) - timedelta(days=a.days))
    r = gather(since)
    print(json.dumps(r, indent=2) if a.json else render(r))
    return 0


# ---------- selftest ----------

def selftest() -> int:
    ok = fail = 0

    def check(c, what):
        nonlocal ok, fail
        ok, fail = (ok + 1, fail) if c else (ok, fail + 1)
        if not c:
            print("FAIL " + what)

    knobs = ("SEAT_LOG_DIR", "SEAT_LOG_PROJECTS", "SEAT_LOG_APPDIR", "SEAT_LOG_TOAST")
    saved = {k: os.environ.get(k) for k in knobs}
    tmp = Path(tempfile.mkdtemp(prefix="seat-log-test-"))
    log, proj = tmp / "log", tmp / "projects" / "C--proj"
    proj.mkdir(parents=True)
    os.environ.update(SEAT_LOG_DIR=str(log), SEAT_LOG_PROJECTS=str(tmp / "projects"), SEAT_LOG_APPDIR=str(tmp / "app"),
                      SEAT_LOG_TOAST="1")
    shown = []
    show = lambda title, text: shown.append(text) or (True, "")
    FABLE, OPUS, SID = "claude-fable-5-1", "claude-opus-5-5", "11111111-2222-3333-4444-555555555555"
    ts = now_iso()[:19] + ".000Z"

    def asst(model, effort, content=(), mid="m1", **extra):
        return {"type": "assistant", "timestamp": ts, "sessionId": SID, "requestedModel": model, "effort": effort,
                "message": {"id": mid, "model": model, "content": list(content),
                            "usage": {"input_tokens": 1, "cache_creation_input_tokens": 10,
                                      "cache_read_input_tokens": 100, "output_tokens": 5}}, **extra}

    human = {"type": "user", "message": {"role": "user", "content": "hello"}}
    result = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}}
    meta = {"type": "user", "isMeta": True, "message": {"role": "user", "content": "caveat"}}

    def write(path, entries):
        path.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries), encoding="utf-8")
        return path

    def events():
        try:
            return [json.loads(x) for x in (log / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        except OSError:
            return []

    def payload(path, **kw):
        return json.dumps({"session_id": SID, "transcript_path": str(path), "cwd": "C:\\proj", **kw})

    def text(t):
        return {"type": "text", "text": t}

    try:
        # the direction table
        for child, parent, want in [(("opus", "high"), (FABLE, "max"), "cheaper"), ((FABLE, "high"), (FABLE, "max"), "cheaper"),
                                    (("opus", "max"), (FABLE, "max"), "cheaper"), ((FABLE, "max"), ("opus", "high"), "dearer"),
                                    ((FABLE, "max"), (FABLE, "max"), "same"), (("haiku", "max"), (FABLE, "high"), "mixed"),
                                    (("gpt-6", "low"), (FABLE, "max"), "unknown"), (("opus", None), (FABLE, "max"), "cheaper"),
                                    ((FABLE, None), (FABLE, "max"), "same"), (("mythos-1", None), ("sonnet", "low"), "dearer")]:
            check(direction(child, parent) == want, f"direction {child} against {parent} is {want}")

        # the seat from a transcript: synthetic skipped, the tail read, the whole-file fallback
        t1 = write(proj / f"{SID}.jsonl", [human, asst(FABLE, "max"), asst("<synthetic>", None, mid="m2")])
        check(read_transcript(t1)[0] == (FABLE, "max"), "seat read, synthetic entry skipped")
        pad = [{"type": "progress", "data": "x" * 1000}] * 300
        t2 = write(tmp / "big.jsonl", [asst(OPUS, "low")] + pad + [asst(FABLE, "high", mid="m3")])
        check(read_transcript(t2)[0] == (FABLE, "high"), "seat read from the tail of a 300 KB transcript")
        t3 = write(tmp / "big2.jsonl", [asst(OPUS, "low")] + pad)
        check(read_transcript(t3)[0] == (OPUS, "low"), "the 8 MB read when the first tail has no assistant entry")
        t4 = write(tmp / "big3.jsonl", [asst(OPUS, "low")] + pad * 30)
        check(t4.stat().st_size > TAIL_MAX and read_transcript(t4, need_turn=True) == (None, []),
              "a seat further back than 8 MB is given up, the whole file never read")

        # delegate
        n = hook(payload(t1, hook_event_name="PreToolUse", tool_name="Agent",
                         tool_input={"description": "read logs", "subagent_type": "general-purpose", "model": "opus",
                                     "effort": "high"}), show)
        ev = events()[-1]
        check(ev["event"] == "delegate" and ev["parent"] == {"model": FABLE, "effort": "max"}
              and ev["child"]["model"] == "opus" and ev["child"]["effort"] == "high" and ev["direction"] == "cheaper",
              "delegate event with parent and child")
        check(n == f"Seat log: delegated to opus at high (this session: {FABLE} at max): read logs", "delegate message")
        check(len(shown) == 1, "cheaper delegate notifies")
        n = hook(payload(t1, tool_name="Agent", tool_input={"description": "same seat"}), show)
        check(n is None and events()[-1]["direction"] == "same" and len(events()) == 2, "default child: same, no message")
        n = hook(payload(t1, tool_name="Agent", tool_input={"subagent_type": "fork", "model": "haiku"}), show)
        ev = events()[-1]
        check(n is None and ev["child"]["fork"] and ev["child"]["model"] == FABLE and ev["direction"] == "same",
              "fork keeps the parent's seat")
        check((log / "events.jsonl.lock").exists(), "lock file created")

        # set_seat
        n = hook(payload(t1, hook_event_name="PreToolUse", tool_name="mcp__ccd_session_mgmt__set_session_model",
                         tool_input={"session_id": "local_abc", "model": FABLE}), show)
        ev = events()[-1]
        check(ev["event"] == "set_seat" and ev["target"] == "local_abc" and ev["model"] == FABLE, "set_seat event")
        check(n == f"Seat log: this session set local_abc's model to {FABLE} by tool" and len(shown) == 2,
              "set_seat message and notification")

        # Stops: first saves, same seat nothing, cheaper logs and notifies, dearer does not notify
        before = len(events())
        stop = payload(t1, hook_event_name="Stop", stop_hook_active=False)
        check(hook(stop, show) is None and hook(stop, show) is None and len(events()) == before,
              "two Stops on one seat log nothing")
        check(load_state(state_path(SID)).get("seat") == [FABLE, "max"], "Stop saves the seat")
        write(t1, [human, asst(OPUS, "high")])
        n = hook(stop, show)
        ev = events()[-1]
        check(ev["event"] == "seat_change" and ev["direction"] == "cheaper" and ev["to"]["model"] == OPUS,
              "seat_change on a cheaper seat")
        check(n == f"Seat log: this session moved from {FABLE} at max to {OPUS} at high" and len(shown) == 3,
              "seat_change message and notification")
        write(t1, [human, asst(FABLE, "max")])
        n = hook(stop, show)
        check(n is not None and events()[-1]["direction"] == "dearer" and len(shown) == 3, "dearer move told, not notified")

        # suggestions
        wrap = {"type": "tool_use", "name": WRAP_UP, "input": {"recap": "Done.", "ask": "Run the sweep on a cheaper seat?",
                                                                 "actions": [{"label": "Yes"}]}}
        write(t1, [asst(FABLE, "max", [text("Old turn: a subagent on opus.")]), human, asst(FABLE, "max", [text(
            "The grep is checkable. I can hand it to an Opus subagent while I keep the design.")]), result,
            meta, asst(FABLE, "max", [wrap])])
        before = len(events())
        hook(stop, show)
        new = events()[before:]
        check([e["where"] for e in new] == ["text", "wrap_up"] and all(e["event"] == "suggest" for e in new),
              "suggestions from text and wrap_up ask, the old turn skipped")
        check(new and new[0]["text"] == "I can hand it to an Opus subagent while I keep the design.", "suggestion sentence")
        hook(stop, show)
        check(len(events()) == before + 2, "the same suggestion is not logged twice")
        check(len(shown) == 3, "suggestions do not notify")
        write(t1, [human, asst(FABLE, "max", [text("Opus is the default model here, and the effort stays at max.")])])
        before = len(events())
        hook(stop, show)
        check(len(events()) == before, "no suggestion on plain mentions")

        # errors: malformed stdin and a missing transcript
        before = len(events())
        for raw in ("{not json", payload(tmp / "missing.jsonl", hook_event_name="Stop")):
            old_in, old_out = sys.stdin, sys.stdout
            sys.stdin, sys.stdout = io.TextIOWrapper(io.BytesIO(raw.encode("utf-8"))), io.StringIO()
            try:
                code, printed = run_hook(show), sys.stdout.getvalue()
            finally:
                sys.stdin, sys.stdout = old_in, old_out
            check(code == 0 and printed == "", f"hook exits 0 quietly on {raw[:12]!r}")
        check(len(events()) == before, "errors write no event")
        check(len((log / "errors.log").read_text(encoding="utf-8").splitlines()) == 2, "one errors.log line each")

        # silenced
        os.environ["SEAT_LOG_TOAST"] = "0"
        check(notify("Seat log: x", show) == (False, "silenced") and len(shown) == 3, "SEAT_LOG_TOAST=0 silences")
        os.environ["SEAT_LOG_TOAST"] = "1"

        # the report
        write(t1, [human, asst(FABLE, "max", [{"type": "tool_use", "name": "Agent",
                                              "input": {"model": "opus", "effort": "high"}}], mid="r1"),
                   asst(FABLE, "max", [text("x")], mid="r1"), asst(OPUS, "high", mid="r2"),
                   asst("<synthetic>", None, mid="r3")])
        (proj / SID / "subagents").mkdir(parents=True)
        write(proj / SID / "subagents" / "agent-a.jsonl", [asst("claude-haiku-4-5", "low", mid="r4", isSidechain=True)])
        app = tmp / "app" / "acct" / "org"
        app.mkdir(parents=True)
        (app / "local_1.json").write_text(json.dumps({"cliSessionId": SID, "title": "Test session"}), encoding="utf-8")
        old_out = sys.stdout
        sys.stdout = io.StringIO()
        try:
            report(["--days", "1"])
            out = sys.stdout.getvalue()
            sys.stdout = io.StringIO()
            report(["--days", "1", "--json"])
            j = json.loads(sys.stdout.getvalue())
        finally:
            sys.stdout = old_out
        for head in ("Turns by seat", "Delegations", "Seat moves", "Suggestions (heuristic)",
                     "Sessions on more than one seat in the window"):
            check("\n" + head + "\n" in out, f"report section {head}")
        fable = next((t for t in j["turns"] if t["model"] == FABLE), {})
        check(fable.get("entries") == 2 and fable.get("responses") == 1 and fable.get("output_tokens") == 5,
              "turns by seat: entries counted, usage once per response")
        check(len(j["turns"]) == 3, "three real seats, synthetic skipped")
        check(j["delegations"]["by_direction"] == {"cheaper": 1, "same": 2} and len(j["delegations"]["cheaper"]) == 1,
              "delegations by direction")
        check(j["delegations"]["agent_calls"] == {"total": 1, "with_model": 1, "with_effort": 1}, "Agent calls counted")
        check(len(j["moves"]) == 3 and len(j["suggestions"]) == 2, "moves and suggestions listed")
        check([m["session"] for m in j["multi_seat"]] == [SID] and len(j["multi_seat"][0]["seats"]) == 2,
              "multi-seat session, the sidechain left out")
        check(os.name != "nt" or "'Test session'" in out, "title from the app's records")
        check(json.loads(json.dumps(j)) == j, "--json round-trips")

        # house form
        raw = Path(__file__).read_bytes()
        check(all(b < 128 for b in raw), "the script is ASCII")
        check(b"\n" not in raw.replace(b"\r\n", b"") or b"\r" not in raw,
              "the script's line endings are all of one kind (CRLF here, LF where a clone checks it out so)")
        check("\u2014".encode("utf-8") not in raw and "\u2013".encode("utf-8") not in raw, "no em or en dash")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    total = ok + fail
    print(f"seat_log selftest: {total} cases, " + ("all passed" if not fail else f"{fail} FAILED"))
    return 1 if fail else 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "hook":
        return run_hook()
    if cmd == "report":
        return report(argv[2:])
    if cmd == "selftest":
        return selftest()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

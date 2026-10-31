# The cheaper seat

Rules for people who run Claude Code on the most capable model at its highest reasoning effort, and a hook that shows
whether the rules are working. A seat is a model at a reasoning effort. Left alone, a session spends the top seat on
everything alike: a security read and a folder listing, a plan and a test run. Claude Code's Agent tool can start a
subagent on another model at another effort, but it sets an effort only when the user or a rules file has asked for
one, so nothing cheaper happens on its own.

The rule is one section you paste into your own `~/.claude/CLAUDE.md`. It splits the work in two. Where something
other than the model's judgment checks the result, a bounded piece goes to a subagent on a cheaper seat and the session
says so in a line. Where the judgment has no check behind it, the top seat stays. For its own session, and for a whole
task, the session suggests the cheaper seat in one line and leaves the change to you. Whatever comes back from the
cheaper seat is checked at the top seat before it counts.

The repository also carries seat-log, a skill with the hook and a report, so that you can see the rule at work
instead of taking it on trust.

## What it looks like without the rule

- Every session runs the top seat, whatever it is doing. A folder listing, a transcript search and a test run cost what
  a design or a security read costs.
- A session never says a piece could run cheaper. Nothing in the harness asks it to, and the Agent tool's effort stays
  unset unless a rules file names one.
- When a session does delegate, nothing records what it delegated or at which seat, so there is no way to tell whether
  a rule about it is followed.

## Install the rule

Paste the whole of [CLAUDE.md](CLAUDE.md) at the end of your own `~/.claude/CLAUDE.md` (on Windows,
`%USERPROFILE%\.claude\CLAUDE.md`), or create that file with it. Claude Code reads the file at the start of every
session in every project. A session that is already running works from the copy it read when it started; ask it to
read the file again, or start a new session.

The section names Opus as the model one tier down, and medium and high as the efforts for delegated work, because that
is the ladder in the Claude 5 family at the time of writing. If your ladder is different, change those words in the
section.

## Why a rules file and not a skill

A skill stays out of context until a task looks like its description. The rule has to act in turns that are about
something else: a session about to spend the top seat on a listing is not thinking about seats. `CLAUDE.md` is in
every session's context from its first turn. The Agent tool also takes an effort for a subagent only when the user or a
rules file has asked for one, never on the session's own judgment, so the sorting of work and the efforts it allows
have to be written where every session reads them.

## Limits

Nothing here is enforced. A model follows written rules most of the time, not every time. The rule names kinds of
work, and a session can misjudge which kind a piece is; the check at the top seat is there for that, and it costs
tokens of its own. A result from the cheaper seat can be wrong in a way the check does not look at. The suggestion the
rule asks for is text, so the hook below can only find it by its wording.

## seat-log

seat-log is a Claude Code hook and a report. The hook reads what the transcripts already record: every assistant entry
carries the model and effort it ran at, and every Agent call carries the model and effort it was given. It logs four
kinds of event to `~/.claude/seat-log/events.jsonl`, one JSON object a line:

- `delegate`: an Agent call, with the session's own seat (read from its transcript), the subagent's seat and the
  direction: cheaper, dearer, same, mixed, or unknown for a model it cannot rank. A fork keeps the parent's seat.
- `set_seat`: a session changing a session's model or effort through the desktop app's tools.
- `seat_change`: the session's own seat at the end of a turn differs from the one at the end of the previous turn,
  which is what happens when you change the model or effort in the app.
- `suggest`: a sentence in the turn's text, or in its end-of-turn question, that suggests a cheaper seat. This is a
  heuristic on wording, logged once per sentence per session.

A cheaper delegate, a set_seat and a cheaper seat_change print a line into the session's transcript and show a
notification (a Windows toast through PowerShell, or macOS's notification through osascript; nothing elsewhere). The
hook never blocks a tool: it logs, prints at most one line, and on any error writes one line to
`~/.claude/seat-log/errors.log` and exits 0.

The report reads the log and the transcripts under `~/.claude/projects` for a window (the last seven days by default)
and prints turns by seat with their token usage, delegations by direction, the count of Agent calls in the transcripts
that named a model or an effort (which reaches back before the hook was installed), seat moves, suggestions, and the
sessions that ran on more than one seat.

    python ~/.claude/skills/seat-log/seat_log.py report --days 7

It installs as a folder under `~/.claude/skills`: copy `skills/seat-log` to `~/.claude/skills/seat-log`, run its
selftest there, then add the hook to `~/.claude/settings.json`. The selftest reads nothing from the live machine and
should end with "seat_log selftest: 54 cases, all passed".

    python seat_log.py selftest

The hook lines, with the path written out in full (put your own user folder in place of `you`):

```json
"hooks": {
  "PreToolUse": [
    {
      "matcher": "Agent|mcp__ccd_session_mgmt__set_session_model|mcp__ccd_session_mgmt__set_session_effort",
      "hooks": [{"type": "command", "command": "python C:/Users/you/.claude/skills/seat-log/seat_log.py hook", "timeout": 20}]
    }
  ],
  "Stop": [
    {"hooks": [{"type": "command", "command": "python C:/Users/you/.claude/skills/seat-log/seat_log.py hook", "timeout": 20}]}
  ]
}
```

If your settings file already has `PreToolUse` or `Stop` entries, add these beside them. Running sessions pick the
change up at their next turn. The script needs Python 3.10 or later and nothing else. It makes no network calls and
writes only under `~/.claude/seat-log`; the session titles in the report come from the desktop app's own records on
Windows, where they exist. Set `SEAT_LOG_TOAST=0` in your environment to keep the log without the notifications.

---

By William Chou ([@willbchou](https://x.com/willbchou))

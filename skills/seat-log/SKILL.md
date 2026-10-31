---
name: seat-log
description: The seat log for Claude Code sessions under the cheaper-seat rule - a hook that logs every seat event it can see (a subagent started on a cheaper or dearer model or effort, a session's own model or effort changed between turns, a seat moved by the desktop app's set-session tools, a sentence suggesting a cheaper seat) to ~/.claude/seat-log/events.jsonl and shows a notification for the cheaper ones, and a report that tallies a window from the log and the transcripts under ~/.claude/projects: turns by seat with their tokens, delegations by direction, seat moves, suggestions, sessions on more than one seat. Use when asked whether the cheaper-seat rule is working, how often work went to a cheaper model or effort, what a session's seat was over a period, or for a week's tally; the hook runs from settings.json, never by hand. Subcommands - report [--days N | --since YYYY-MM-DD] [--json], selftest.
---

# Seat log: the cheaper seat, logged and told

A seat is a model at a reasoning effort. The cheaper-seat rule in CLAUDE.md has a session on the top seat hand
checkable work to a subagent on a cheaper one and suggest a cheaper seat for its own session. This skill is the record
of whether that happens. The hook reads what the transcripts already carry (each assistant entry's model and effort,
each Agent call's model and effort) and writes one JSON object a line to `~/.claude/seat-log/events.jsonl`; the report
reads that log and the transcripts.

## The order

1. `python ~/.claude/skills/seat-log/seat_log.py report --days 7` (or `--since 2026-10-21`; `--json` for the same as one
   object). It reads every transcript under `~/.claude/projects` changed in the window, so a week over many sessions
   takes a minute.
2. Read it in five sections. "Turns by seat" counts assistant entries per model and effort, with the token usage summed
   once per API response, since each entry of one response repeats the same usage. "Delegations" counts the hook's
   `delegate` events by direction and lists the cheaper ones with their descriptions; the line "Agent calls in the
   transcripts" counts, from the transcripts themselves, the Agent calls that named a model and those that set an
   effort, which reaches back before the hook was installed. "Seat moves" lists a session's own seat changing between
   turns and a seat set through the app's tools. "Suggestions (heuristic)" lists sentences matched by wording; a
   session quoting the rule can trip it, so read the sentence. "Sessions on more than one seat" reads a session's own
   entries only, not its subagents'.
3. Say what the numbers show against the rule: delegations with an effort set are the rule's own instruction at work;
   a seat_change is the user's change in the app; a set_seat is a session moving a seat by tool, which the rule
   forbids unasked.

## What the hook does

`seat_log.py hook` is the command in `settings.json`, on PreToolUse for `Agent` and the two set-session tools, and on
Stop. It logs `delegate` (the session's seat from the last real assistant entry of its transcript, against the
subagent's model and effort; a fork keeps the parent's seat), `set_seat`, `seat_change` (the turn's seat against the
one saved at the previous Stop; the first Stop only saves) and `suggest` (once per sentence per session). A cheaper
delegate, every set_seat and any seat_change print one line into the transcript; the cheaper ones and set_seat also
show a notification (Windows toast or macOS notification; `SEAT_LOG_TOAST=0` silences). It never blocks a tool and
exits 0 on any error, with one line in `~/.claude/seat-log/errors.log`. It reads at most the last 8 MB of a
transcript. Knobs, as environment variables: `SEAT_LOG_DIR`, `SEAT_LOG_PROJECTS`, `SEAT_LOG_APPDIR` (the desktop
app's session records, for titles on Windows), `SEAT_LOG_TOAST`.

## Checks

`python seat_log.py selftest`: the direction table, the seat and tail reads, each event kind with its message and
notification, the suggestion patterns and their dedupe, the error path, the report's sections and its JSON, and the
file's own form, and its own temp folder gone at the end: 54 cases, under two seconds, nothing read from the live
machine, no notification shown and nothing left in the temp folder.

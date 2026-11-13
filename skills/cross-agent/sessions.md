# Sessions, status and run control

## Registry and journal

1. Keep a stable conversation ID, canonical repository/issue identity, private registry
   and artifact roots (directories created with `mkdir -m 700`, see
   [protocol.md](protocol.md)). `--task new` separates a task; `--new`
   refreshes only its session. Explicit `--session` has exactly one recipient.
   Incompatible identities block resume. Write each launched run's registry `session_id`
   (and `cli_session_id` when known), the role and the status into the host's own journal
   at launch, not afterwards: resuming a session later needs the ID, and `--status` lists
   every session of the repository.

## Adopting a provider session

1. The subcommand form is `adopt <uuid> '<request>'`; `--adopt-cli-session` stays as the
   compatibility spelling. `--adopt-cli-session <uuid>` needs the bracket
   request (`'claude opus low [<skill> <brief>]'`) too; without one it answers `BLOCKED`
   with the expected form. It binds a provider session started outside the launcher (for
   example an interactive Claude Code session) and prints its registry session ID; a
   Claude Code session must exist in the workspace's project, or adoption is refused. The
   same provider ID under another agent or canonical issue is refused. The first run
   after adoption reports unknown round usage (`adopted session: no baseline`) and keeps
   the cumulative figures as the session total and the baseline for later runs. An
   adopted Codex session keeps measured rounds, but its session total stays incomplete.

## Status

1. `--label <text>` (at most 40 characters) tags a run, and `--status` reads the registry
   without writing it and prints the repository's sessions: state, PID, elapsed time
   (until `finished_at` for a finished run) and, for Claude Code, the last main-thread
   message and tool call from its transcript. A fresh Claude Code run records the
   `--session-id` it was launched with as `launch_cli_session_id` on the run, so
   `--status` reads its transcript while it runs; that ID is never a resumable or owning
   binding. Transcripts are found through the child environment: `CLAUDE_CONFIG_DIR`,
   else its `HOME`/.claude. A Claude Code row also has `estimate`: tokens and cost summed
   from the main and subagent transcripts (see [CLI runtime](cli-runtime.md)).
   It is not billing data, covers the interactive part before adoption too, and stays
   separate from the launcher's exact `usage.json` figures.
2. `status` prints one line per session (the last two columns are the label and the
   `result.json` path, `-` when empty); `--json` prints the JSON of `--status` (also kept
   as the compatibility spelling); `--session <id>` and `--active` filter. Every row has
   `result_path`: the `result.json` of the latest run, or null.

## Cancel and wait

1. `cancel <session_id>` signals the launcher process of the session's running run with
   SIGTERM after checking that the PID is alive and that the first argument without `-` after
   the interpreter in its command line is a path ending in `cross_agent.py` (a path with
   spaces is refused, never signalled). The launcher then stops the child process group and
   records `CANCELLED` (reason `SIGNAL`); a signal that lands before the child starts is
   recorded the same way. Refusals are `BLOCKED` with the reason
   `UNKNOWN_SESSION`, `NOT_RUNNING` (also when the process vanishes before the signal) or
   `PID_MISMATCH`. Only the same OS user can signal, and a reused PID is a
   residual risk that the command-line check lowers but does not remove.
2. `wait <session_id> --timeout <seconds>` polls the registry every 2 seconds until the run
   leaves `running` (or its process is gone) and its `result.json` exists on disk, then prints
   the status row with `waited_seconds` and `timed_out`. The launcher writes `result.json`
   after it records the state, so a finished run without the file keeps `wait` polling until
   the timeout. Exit code 0 means finished, 3 a timeout, 2 `BLOCKED`, and 1 a run that
   ended with no `result_path` recorded, for example a launcher that died first (the row has
   `result_missing: true`; no result will appear).

## Dry run and host defaults

1. `--dry-run` resolves agent, model, effort, role, profile and `granted` for each
   recipient without running anything: no registry, artifacts or processes are created.
   The exit code is 1 when a recipient is unavailable.
2. Without `--host-context` the launcher reads `<state>/host.json` (created by `init-host`,
   mode 0600) or builds a default host JSON. `<state>` is
   `~/.sentimony/cross-agent/cross-agent/<repository directory name>-<12 hex digits of the
   sha256 of the resolved repository path>/`. A stored `host.json` whose `workspace` or
   `task_context.repo` is another repository is refused.
3. The default `target_identity` is `tree`, `head` and `status_digest` (a sha256 of
   `git status --porcelain=v1 -z -uall`). A change inside a file that is already dirty is not
   visible in it; for an exact tree pass your own host JSON.
4. The host family comes from `CLAUDE_CODE_SESSION_ID` (`claude-code`) or `CODEX_THREAD_ID`
   (`codex`; this variable is not verified on a live Codex). Otherwise pass `--host-family`
   and `--conversation-id`.

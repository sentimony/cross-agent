# Changelog

## 0.0.4 - 2026-11-05

1. `cross-agent` 0.0.4: accept bounded UTF-8 assignment files with exact content.
2. `context-transfer` 0.0.3: capture linked worktrees and local refs, and report drift and unverified facts.

## 0.0.3 - 2026-11-04

1. `cross-agent` 0.0.3: document that `AMBIGUOUS_SESSION` is resolved with `--new` or
   `--session <id>` inside the request string (request `20261104-0925`).
2. `context-transfer` 0.0.2: document the default context usage thresholds (prepare 50%,
   refresh 62%, rotate 75% of the window; 100000, 125000 and 150000 tokens when the window
   is unknown) and how to override them (request `20261103-1917`).

## 0.0.2 - 2026-11-04

1. Ship an owner-attested `config/default.json` that the launcher uses when `--config`
   is omitted: Claude Code (Opus 5.5 default, Fable 5.1, Sonnet 5.5) reviews, Codex
   (`gpt-6.1-sol` default, `gpt-6.1-terra`) reviews or executes, both at `medium` effort
   by default. An explicit `--config` replaces it.
2. Add a Claude Code execution profile for delegated orchestrators: an explicit
   `orchestrate` host action, project settings loaded, workspace tools and subagents,
   no MCP or web tools. Host JSON `pass_env` keeps named variables in the child.
3. Make Claude Code sessions resumable with the real `claude -p` envelope: the model
   comes from `modelUsage`, and resume no longer requires an effort Claude never reports.
4. Add `--adopt-cli-session <uuid>`: bind an existing provider session to the registry
   so the launcher can resume it; a second owner of the same provider ID is refused,
   and a Claude Code session missing from the workspace's project is refused.
5. Add `--status`: sessions of the repository with state, PID, elapsed time and the last
   main-thread message and tool call of a Claude Code session.
6. Tell orchestrators to ask the owner through `USER_INPUT_REQUIRED` with a handoff, and
   give `BLOCKED` results the first output line as their reason.
7. Limit Claude Code execution tools with `--tools` as well as `--allowedTools`, so
   inherited settings cannot make an unlisted tool available.
8. Record `model_basis` in the observed identity (`launch-flag+modelUsage` for Claude
   Code, `provider` when the envelope names the model).
9. Measure Claude Code resume rounds again: `identity_attested` ignores the effort that
   `claude -p` never reports.
10. Mark adopted sessions; their first run reports unknown round usage and keeps the
    cumulative figures as the session total and later baseline. An adopted session
    with per-round usage (Codex) keeps measured rounds but never a complete session total.
11. Find Claude Code transcripts for adoption and status through the child environment.
12. Make `--status` read-only and stop the elapsed time of a finished run at `finished_at`.
13. Refuse adopting a provider session already owned under another canonical issue.
14. Report `skills_note: not detected` for Claude Code execution usage.
15. Show an `estimate` of tokens and cost per Claude Code session in `--status`, summed
    from its main and subagent transcripts; not billing data. Unreadable files and rows
    without input/output counts mark it `complete: false`; the largest copy of a
    message counts once.
16. Record the `--session-id` of a fresh Claude Code run before it starts, so
    `--status` shows its last message, tool and estimate while it runs; the ID stays a
    run field and never becomes a resumable or owning binding.
17. Read Codex model and effort from the child's rollout `turn_context`, so a completed
    Codex session can resume (`model_basis: provider-rollout`).
18. Keep `CONSULTATION_REQUIRED`/`USER_INPUT_REQUIRED` with a malformed `handoff` as
    the child's status plus `handoff_invalid: true` instead of `FAILED`; the brief
    shows an exact `handoff` JSON example.
19. Add the separate context-transfer skill (0.0.1) for saving and resuming
    session checkpoints, with an `argument-hint`, ready `resume <id>` commands after `write`
    and a validation message that names the index and type of an invalid `artifacts` entry
    (requests `20261104-0508`, `20261104-0500`).
20. Accept a child result wrapped in one fenced code block (with or without a `json` tag,
    prose around the block is discarded) and report an unparseable result as `child result
    is not JSON v1: <first 80 characters>`; the child brief now asks for raw JSON (request
    `20261103-2343`).
21. `context-transfer` stores checkpoints outside the project by default, in
    `~/.sentimony/cross-agent/context-transfer/<slug>/` (slug of the main checkout, shared by
    all worktrees), so nothing is added to the project or to `.git/info/exclude`; a legacy
    store in the common Git directory or in `docs/context-transfer/` is used only while the
    new store holds no checkpoints (request `20261104-0624`, `cross-agent` part pending).
22. Document in `cli-runtime.md` that Codex execution needs its own host policy
    (`codex`, `write-workspace`, isolated config, `repo` equal to `workspace`, fresh
    evidence), and in `configuration.md` that a `skill` assignment starts with the
    installed skill name.
23. Tell hosts to journal each launched run's registry `session_id` at launch, and
    document in `configuration.md` that the outer flags (`--session`, `--task`, `--new`)
    go inside the request string, not as separate launcher arguments (`unrecognized
    arguments`), and that `--session` takes the registry `session_id`, not the
    provider's `cli_session_id`.
24. Add a minimal working host JSON example to `protocol.md`, with the optional fields
    (`assignment_kind`, `installed_skills`, `policy`, `answer`) listed separately.
25. `context-transfer` `write`, `facts` and `resume-check` report the store in use
    (`store: default`, `legacy (<path>); default store is empty, ...`, `custom (<path>)`),
    so a legacy store is never used silently.
26. Name the limits of the Codex `execution` profile in `cli-runtime.md` (`workspace-write`
    sandbox, no network, no `.git` writes) and add a "Prepare" rule in `SKILL.md`: tasks
    needing network, dependency installation or commits go to a Claude Code executor
    (request `20261104-1109`, items 1-2; the launcher warning, item 3, is pending).

## 0.0.1 - 2026-11-01

1. Prepare a standalone plugin with generic bracket parsing, explicit agent profiles,
   task/session registry, provider provenance, launcher transport and protocol v1.
2. Resolve host-allowlisted skill resources from the installed source; connect domain
   policy through an explicit installed owner adapter, with trusted opaque context
   and typed pre-dispatch decisions.
3. Native installation, live profiles and delivery remain unverified. The owner
   accepted this initial-release risk, tracked in
   https://github.com/sentimony/agentic-workflow/issues/31.

4. Add independent fake-only CI and one canonical private-environment test entrypoint;
   document and test registry identity across installation-root relocation.

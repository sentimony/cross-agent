# Changelog

## 0.0.8 - 2026-11-12

1. Add the profiles `full` (the default for an execution run), `no-network` (the former
   `execution`, still read as that spelling) and `read-only`. `--mode` narrows the host
   profile and never widens it; a consultation or review host stays `read-only`
   (`20261106-2033`, `20261105-2203`).
2. Codex `full` runs with `-s danger-full-access`; Claude Code `full` adds `WebFetch` and
   `WebSearch` to the `no-network` tools and keeps MCP denied. Neither is verified against a
   live CLI. `full` needs no host `policy`; `no-network` still does.
3. Replace the bundled model set: `claude-opus-5-5`, `claude-sonnet-5-5`, `gpt-6.1-sol`,
   `gpt-6.1-terra`, and `gemini-4-argon`, `gemini-3.8-flash` (pending live probe), each with
   efforts `low`, `medium`, `high`. `claude-fable-5-1`, `xhigh` and `max` are gone from the
   default. A config written for 0.0.7 has no `full` profile and returns `UNAVAILABLE` for a
   default run until `full` is added.
4. Add the optional `subagent_models` field, `claude-haiku-5-5` and `gpt-6.1-lune` as
   subagent-only models, and the `lune` alias. The brief lists the allowed subagent models;
   the result records `subagents`. Default recipients skip families whose profile is not
   `supported`.
5. Add the subcommands `run`, `status`, `adopt`, `init-host`, `cancel` and `wait`, a
   host JSON built from the repository, separate `--task`/`--session`/`--new`,
   `--assignment-file -`, `--dry-run`, `status --json --session --active` and `result_path`
   in status rows. `--status` and `--adopt-cli-session` stay one more release
   (`20261106-2033`, `20261004-0011`).
6. Record `profile`, `granted` and `actions_observed` in every result and warn about the
   Codex `no-network` sandbox (`20261104-1109`).
7. Move the registry, adoption, status and cost-estimate text from `SKILL.md` to
   `references/sessions.md`.
8. Harden the new surface: the state directory must be a real directory with mode 700,
   `init-host --force` stages through an exclusive temporary file, `wait` exits 1 with
   `result_missing` when a run ended without a result path, and a `cancel` that lands
   before the child starts is recorded as `CANCELLED`.

## 0.0.7 - 2026-11-10

1. Name the bracketed assignment form in every request parse error, list the installed
   skill names when the first word is not one, and show a double-quoted dispatch example
   (`20261104-1532`).
2. Report the path, mode and owner in `UNAVAILABLE`/`UNSAFE_REGISTRY_PATH` registry errors;
   `--adopt-cli-session` without a request answers `BLOCKED` with the expected form
   (`20261104-1654`).
3. Warn in the dispatch result when `artifact_root` or `registry_path` lies under a
   temporary directory (`20261104-0624`).
4. Name the empty evidence fields as `missing_fields` when a profile policy is incomplete,
   and document how to verify a profile (`20261104-1631`).
5. Check the optional `required_roots` against the Claude Code read-only profile before
   launch, and add a profile capability table to `cli-runtime.md` that marks only
   evidenced limits as verified (`20261104-1616`).
6. Add `--label` (at most 40 characters, no control characters) stored on the run and shown
   in `--status` rows, and document the status row fields (`20261104-0010`).
7. Record the Codex `rate_limits` snapshot of the last `token_count` event in `usage.json`
   (`20261025-1722`).

## 0.0.6 - 2026-11-07

1. Name `transfer-context`, the new name of `context-transfer`, for handing state to a new session.

## 0.0.5 - 2026-11-07

1. Document that `registry_path` names a directory holding `registry.json` and
   `registry.lock`, and that the registry and artifact roots are created with
   `mkdir -m 700` (`20261104-1654`, text part).
2. The `BLOCKED` and `UNAVAILABLE` reasons show the bracketed assignment form and say that the
   first word in brackets is read as an installed skill name (`20261104-1532`, text part).

## 0.0.4 - 2026-11-05

1. Accept bounded UTF-8 assignment files without changing brackets or line endings.

## 0.0.3 - 2026-11-04

1. Document how to resolve `AMBIGUOUS_SESSION` inside the request string.

## 0.0.2 - 2026-11-04

1. Ship an owner-attested `config/default.json` that the launcher uses when `--config`
   is omitted: Claude Code (Opus 5.5 default, Fable 5.1, Sonnet 5.5) reviews, Codex
   (`gpt-6.1-sol` default, `gpt-6.1-terra`) reviews or executes, both at `medium` effort
   by default. An explicit `--config` replaces it.
2. Read Codex model and effort from the child's rollout `turn_context`, so a completed
   Codex session can resume (`model_basis: provider-rollout`).
3. Keep `CONSULTATION_REQUIRED`/`USER_INPUT_REQUIRED` with a malformed `handoff` as the
   child's status plus `handoff_invalid: true` instead of `FAILED`; the brief shows an
   exact `handoff` JSON example.
4. Accept a child result wrapped in one fenced code block (with or without a `json` tag,
   prose around the block is discarded) and report an unparseable result as `child result
   is not JSON v1: <first 80 characters>`; the child brief now asks for raw JSON (request
   `20261103-2343`).
5. Add a Claude Code execution profile for delegated orchestrators (`orchestrate` host
   action, project settings, workspace tools and subagents, no MCP or web tools); host
   JSON `pass_env` keeps named variables in the child; limit tools with `--tools` as
   well as `--allowedTools`.
6. Make Claude Code sessions resumable with the real `claude -p` envelope, record
   `model_basis` in the observed identity and measure resume rounds without an effort
   Claude never reports.
7. Add `--adopt-cli-session <uuid>` to bind an existing provider session to the registry,
   with refusal of a second owner, another canonical issue or a Claude Code session
   missing from the workspace's project.
8. Add a read-only `--status`: sessions of the repository with state, PID, elapsed time,
   the last message and tool call, and an `estimate` of tokens and cost per Claude Code
   session; a fresh Claude Code run records its `--session-id` before it starts.
9. Tell orchestrators to ask the owner through `USER_INPUT_REQUIRED` with a handoff, and
   give `BLOCKED` results the first output line as their reason.
10. Document the Codex execution host policy and sandbox limits (`workspace-write`, no
    network, no `.git` writes), a "Prepare" rule that routes such tasks to a Claude Code
    executor, journaling of session IDs, the outer-flag form of `--session`, `--task` and
    `--new`, and a minimal working host JSON example in `protocol.md`.

## 0.0.1 - 2026-11-01

1. Extract generic delegation from the unpublished embedded workflow prototype.
2. Separate domain context, result validation and intake through owner protocol v1;
   preserve opaque trusted owner context/selectors and typed prepare stops before state/model.
3. Preserve task/provider identity and nested-delegation checks; add installed resources.

4. Add independent fake-only CI and one canonical private-environment test entrypoint;
   document and test registry identity across installation-root relocation.

# Cross Agent protocol v1

1. Protocol and owner contract versions are integer `1`. Generic result statuses are
   `COMPLETED`, `CONSULTATION_REQUIRED`, `USER_INPUT_REQUIRED`, `BLOCKED`, `FAILED`,
   `UNAVAILABLE`, `CANCELLED`. Unknown provider identity and usage remain explicit.
2. Host JSON contains `task_context` (`registry_path`, `repo`, `conversation_id`,
   optional active task IDs/canonical issue), `host_family`, `workspace`,
   `artifact_root`, `target_identity`, trusted `assignment_kind`, `installed_skills`,
   `role`, optional `profile` (the host's ceiling: `read-only`, `no-network`, `full` or the
   legacy `execution`; default `full` for `role: execution`, `read-only` otherwise),
   optional timeout/policy/environment/`pass_env` (variable names kept
   in the child), answer and opaque `owner_context` object. CLI configuration is a
   separate explicit schema1 file; child text cannot supply host fields. Hosts should put
   `registry_path` and `artifact_root` under
   `~/.sentimony/cross-agent/cross-agent/<project>/`, not into an OS temporary directory,
   which a reboot clears; nothing then lands in the project tree. The launcher does not
   choose these paths, but it adds `registry_path under a temporary directory: <path>` or
   `artifact_root under a temporary directory: <path>` to the `warnings` list of the
   dispatch result when one of them is under `/tmp`, `/private/tmp`, `/var/tmp` or the
   system temporary directory; the status does not change. Both are directories:
   `registry_path` holds `registry.json` and `registry.lock`, and `artifact_root` holds
   one folder per run. Create both with `mkdir -m 700` (the parents must already exist),
   for example `mkdir -m 700 <registry_path>`. The launcher answers
   `UNSAFE_REGISTRY_PATH: <label> <path> mode <mode>, expected 700` (or `600` for
   `registry.json` and `registry.lock`) for a registry directory or file that group or
   others can access: run `chmod 700 <dir>` for the directory, `chmod 600 <file>` for a
   file. For `owner uid <n>, expected uid <m>` chmod does not help: the path must belong
   to the current user, so fix its ownership or choose another registry root. The
   launcher also answers `unsafe run root (mode 755, expected 700)` for an
   `artifact_root` whose mode is not exactly 700. Keep the parent directories under
   `~/.sentimony` closed to group and others too; the launcher does not check their mode.
   Minimal read-only consultation host JSON (`policy`, `answer` and `owner_context` are
   omitted; `host_family` is the host's own family, `claude-code` or `codex`):

   ```json
   {
     "task_context": {"registry_path": "<state>/registry", "repo": "<repo root>",
                      "conversation_id": "<host conversation id>"},
     "host_family": "claude-code",
     "workspace": "<repo root>",
     "artifact_root": "<state>/artifacts",
     "target_identity": {"tree": "<tree fingerprint>"},
     "role": "consultation",
     "profile": "read-only"
   }
   ```

   Add `assignment_kind: "skill"` and
   `installed_skills: {"<name>": {"origin": "<origin>", "base": "<absolute skill dir>"}}`
   to run an installed skill. The `no-network` profile also needs `policy` (`family`, `repo`,
   `fresh_evidence`, `resume_evidence`, `local_actions`, `delegation_blocked`,
   `config_isolated`; see [CLI runtime](cli-runtime.md)). When evidence is empty, the
   launcher answers
   `UNAVAILABLE: execution needs verified scoped host policy; missing_fields: ["fresh_evidence"]`
   (and `"resume_evidence"` for a resume). `answer` carries the host's reply to a prior
   `USER_INPUT_REQUIRED` handoff.
3. Generic `DispatchResult` schema1 preserves task ID, target identity and recipient
   results. Each run records host session/run IDs, provider ID, actual model/effort,
   role/profile, operation, status, artifacts and usage. Registry schema1 remains
   compatible with the embedded checkpoint; installation paths do not define task or
   provider identity. A Codex run's `usage.json` also has `rate_limits`: the last rollout
   `token_count` event's snapshot taken after the run (`primary`, `secondary`,
   `plan_type`), or `null` when that event carries none, without data, and for other
   families. There is no snapshot before the run, so no delta or warning threshold.
   Each recipient result has `profile` (canonical name), `granted` (`write`, `network`,
   `git_write`, `mcp`, `basis`; null for a family without an adapter), `actions_observed`
   (always null: actions are not detected) and `subagents` (`observed`, `unexpected`; null
   when the adapter cannot see them). A Codex `no-network` recipient adds
   `codex no-network: sandbox workspace-write, no network, no write access to .git` to the
   dispatch `warnings`.
4. `integration_required` or an installed skill's equivalent flag requires `integration`.
   This binding contains stable `owner_id`, `adapter_id`, integer `contract_version: 1`,
   `protocol_version: 1`, and `capabilities: ["prepare", "validate", "intake"]`.
   The host separately allowlists `installed_root`, absolute `entrypoint` below that root,
   and an absolute trusted `interpreter`. No sibling discovery or response callback exists.
5. The launcher executes `[interpreter, entrypoint, "--stage", stage]` with JSON stdin.
   The request has `schema_version: 1`, `protocol_version: 1`, owner/adapter/contract/stage,
   host task kind/conversation/repo/role/operation/profile/target identity and domain `payload`.
   Once known, task/session/run/provider IDs are included for validation/intake.
   The reserved top-level `owner_context` is a JSON copy of trusted host input;
   only the installed owner interprets its domain keys, such as review records,
   current task ID, review path/intake identity or artifact root. Core does not infer them.
   Reserved `task_selection` contains the parsed `task_selector`, `session_selector`
   and `new_session`. Both fields remain outside domain payload and child output.
   Payload keys cannot overwrite host-owned fields.
6. Each response has exactly `schema_version`, `protocol_version`, `owner_id`, `adapter_id`,
   `contract_version`, `stage`, `payload`; metadata must match the trusted binding.
   Normal `prepare.payload` has exactly `accepted: true` and a text `context`.
   An early stop instead has `accepted: false`, `status` equal to `USER_INPUT_REQUIRED`,
   `BLOCKED` or `UNAVAILABLE`, and a nonempty text `reason`; optional `handoff` is an
   object and `evidence` is a list. Extra/host-overriding fields are rejected.
   Typed stops return before any registry mutation or model launch.
   `validate.payload` and `intake.payload` each require `accepted: true`.
   Prepare receives assignment data; validate/intake receive `result` evidence.
7. Prepare runs before registry mutation/model launch. For successful domain results,
   validate and intake run before final `COMPLETED`; either rejection yields `FAILED` and
   retains raw artifacts. A transport failure is never upgraded by the owner adapter.
8. The installed owner owns domain policies, target freshness and intake ambiguity.
   Missing/incompatible binding or capability returns `UNAVAILABLE` before model/state.
   Follow-up generic consultation can use a compatible reviewer session without owner policy.
9. Adapter stderr/invalid response is a failure, not executable content. Native metadata,
   adapter authenticity and external service behavior require separate evidence.

## Subcommands

1. `run` prints the `DispatchResult`.
2. `run --dry-run` prints `{"schema_version": 1, "dry_run": true, "recipients": [...], "warnings": [...]}`.
3. `status` prints one line per session, or the rows below with `--json`.
4. `init-host` prints `{"schema_version": 1, "status": "COMPLETED", "host_context", "state_dir"}`.
5. `cancel` prints `{"schema_version": 1, "status": "COMPLETED", "action": "cancel", "session_id",
   "pid", "signalled": true}`, or `BLOCKED` with `UNKNOWN_SESSION`, `NOT_RUNNING` or `PID_MISMATCH`.
6. `wait` prints the status row with `waited_seconds` and `timed_out`; the exit code is 0
   (finished), 3 (timeout) or 2 (`BLOCKED`).

## Status rows

`--status` prints `{"schema_version": 1, "sessions": [...]}` without writing the registry:
one row per session of the repository, newest run first. Each row has these fields; a
field that does not apply is `null`.

| Field | Meaning |
| --- | --- |
| `session_id` | Registry session ID. |
| `task` | Canonical issue, else the task ID. |
| `family`, `model`, `effort` | The session's agent identity. |
| `cli_session_id` | Provider session ID, bound or observed. |
| `launch_cli_session_id` | ID the launcher gave a running fresh Claude Code run. |
| `state` | Session state: `idle`, `running`, `completed`, `failed` or `blocked`. |
| `label` | The latest run's `--label` text. |
| `pid`, `alive` | Latest run's launcher PID; `alive` is checked only while the run is `running`. |
| `started_at`, `elapsed_seconds` | Start time and duration (until `finished_at` once finished). |
| `last_text`, `last_tool`, `last_at` | Last main-thread message, tool call and time (Claude Code). |
| `estimate` | Token and cost estimate from the transcripts (Claude Code), not billing data. |
| `result_path` | `result.json` of the latest run, or null. |

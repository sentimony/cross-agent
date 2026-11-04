# Cross Agent protocol v1

1. Protocol and owner contract versions are integer `1`. Generic result statuses are
   `COMPLETED`, `CONSULTATION_REQUIRED`, `USER_INPUT_REQUIRED`, `BLOCKED`, `FAILED`,
   `UNAVAILABLE`, `CANCELLED`. Unknown provider identity and usage remain explicit.
2. Host JSON contains `task_context` (`registry_path`, `repo`, `conversation_id`, optional
   active task IDs/canonical issue), `host_family`, `workspace`, `artifact_root`,
   `target_identity`, trusted `assignment_kind`, `installed_skills`, `role`, `profile`,
   optional timeout/policy/environment/`pass_env` (variable names kept in the child), answer and opaque `owner_context` object.
   CLI configuration is a separate explicit
   schema1 file; child text cannot supply host fields.
   Hosts should put `registry_path` and `artifact_root` under `docs/cross-agent/` of the
   main checkout and add that directory to `.git/info/exclude`, not into an OS temporary
   directory, which a reboot clears. The launcher does not choose or check these paths.
3. Generic `DispatchResult` schema1 preserves task ID, target identity and recipient results.
   Each run records host session/run IDs, provider ID, actual model/effort, role/profile,
   operation, status, artifacts and usage. Registry schema1 remains compatible with the
   embedded checkpoint; installation paths do not define task or provider identity.
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

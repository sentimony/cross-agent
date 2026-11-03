# Checkpoint format

A checkpoint is Markdown with a JSON object between `---` front matter lines. Write a
UTF-8 draft with only the model-owned fields `task`, `verification`, `continuation`,
`artifacts` and optional `source.model`. `write` supplies all other fields and rejects
script-owned fields in a draft. Artifact paths are relative to the project root and must
exist when writing; describe missing paths in `Missing information` instead.

The model owns `task.objective`, `task.status` (`in-progress`, `blocked`, `awaiting-user`,
`done`), `task.next_action`, `verification.status` (`verified`, `partial`, `unverified`),
`verification.evidence` entries (`claim`, `check`, `result`), and the lists
`continuation.recommended_skills`, `continuation.blockers`, and
`continuation.pending_decisions`. It also owns `artifacts` and optional `source.model`.

The script owns `schema_version`, `transfer_id`, `created_at`, `previous_transfer_id`,
`source.agent`, `source.session_id`, `source.context`, `project`, and
`delegated_sessions`. Project facts include root, worktree, Git common directory, remote,
branch, HEAD, dirty and untracked paths, and content digests. Delegated sessions contain
session identity, task, family, model, effort, CLI session, state and liveness, or a
`not_captured` reason. Source context records occupancy, window, percent, basis and
threshold mode. Unknown values are null.

Use the following section order. Keep empty sections as `None.`. `write` appends a
self-contained block with the checkpoint's absolute path and manual Git checks to the
resume prompt.

```markdown
---
{
  "task": {"objective": "Finish the parser", "status": "in-progress", "next_action": "Inspect the failing case"},
  "verification": {"status": "partial", "evidence": [{"claim": "Parser handles empty input", "check": "python3 -m unittest", "result": "passed"}]},
  "continuation": {"recommended_skills": [], "blockers": [], "pending_decisions": []},
  "artifacts": [],
  "source": {"model": "example"}
}
---
## Resume prompt
Continue the parser work after checking the current project state.
## Objective and acceptance
Handle empty and malformed input.
## Authority and prohibitions
No deployment authorized.
## State
The empty input case passes.
## Decisions
None.
## Running now
None.
## Pending user decisions
None.
## Failed approaches
None.
## Verification
The recorded unit test passed in this session.
## Next actions
Inspect the failing case.
## Missing information
None.
```

By default Git worktrees share `<git common dir>/context-transfer/`. Outside Git the store
is under `$XDG_STATE_HOME/context-transfer/<slug>/`, or `~/.local/state/context-transfer/<slug>/`.
`--store` overrides `CONTEXT_TRANSFER_DIR`, which overrides the default. Each checkpoint
is `<transfer_id>.md`; `latest-<worktree slug>` points to the latest one. Optional
`config.json` holds thresholds and `window_tokens`; hooks use `reminders/`.

Run `validate <path>` to check a completed checkpoint. Missing artifacts are warnings
on validation and errors on write. `--budget` sets the body character warning threshold
(default 12000). The script rejects known secret patterns; review all material for other
secrets before writing.

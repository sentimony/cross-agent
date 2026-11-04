# CLI profile evidence

1. Configured read-only and execution profiles require explicit host evidence. Native
   CLI presence, a model name or login state is insufficient. `requires-live-probe`
   and `unavailable` configurations never silently become supported.
2. The extracted command builders preserve the embedded source's explicit model/effort,
   permissions and child environment contracts. They have fake-CLI fixture coverage;
   this standalone candidate has no native version/install/invocation matrix.
3. The bundled `config/default.json` marks the Claude Code read-only and Codex
   read-only/execution profiles `supported` on the owner's attestation; it is not probe
   evidence. Replace it with an explicit `--config` where that attestation does not hold.
   No production execution policy is shipped.
   Codex execution needs its own host policy, not the orchestrator one: `family: codex`,
   `local_actions: ["read", "write-workspace"]`, `delegation_blocked: true`,
   `config_isolated: true`, `repo` equal to the resolved `workspace`, and non-empty
   `fresh_evidence`. A Claude Code policy (`family: claude-code`, `orchestrate`) makes the
   launcher return `UNAVAILABLE: execution needs verified scoped host policy`.
   The Codex execution child runs with `-s workspace-write` (`sandbox_mode="workspace-write"`
   on resume). That sandbox permits file edits in the workspace and local test runs, but in
   an observed run it gave no network (`pnpm install`, `gh` and `git fetch` fail) and no
   write access to `.git` (`git commit` fails); the child then returns
   `USER_INPUT_REQUIRED` and may leave partial artifacts such as a package-manager store.
   Do not assign Codex a task that needs network, dependency installation or commits.
   Claude Code execution is an orchestrator profile: the host policy lists `read`,
   `write-workspace` and `orchestrate`, with `delegation_blocked: true`; config is not
   isolated, because the child loads user, project and local settings (hooks, skills,
   instructions). It runs with `acceptEdits`, prompts denied, and `--tools` equal to
   `--allowedTools` (workspace tools, `Agent` and `Skill`), so inherited settings cannot
   add a tool; no MCP servers and no web tools. The launcher stays blocked inside it.
   `claude -p` reports neither model nor effort fields, and its `modelUsage` also lists
   subagent models. The requested model's presence there is a necessary condition; the
   identity rests on the launcher's own `--model` flag and is recorded as
   `model_basis: launch-flag+modelUsage` (`provider` when the envelope names the model).
   A Claude Code resume and `identity_attested` pin session and model only, because
   effort is never reported. Usage records `skills_note: not detected` for execution and
   `disabled by policy` for read-only. A trusted host may attest bounded local
   workspace actions only after checking isolation, delegation denial and fresh/resume
   behavior. Unknown combinations return `UNAVAILABLE`.
   `codex exec --json` prints no model or effort. The launcher reads them from the last
   `turn_context` of the child's rollout
   (`<CODEX_HOME or HOME/.codex>/sessions/*/*/*/rollout-*-<thread_id>.jsonl`) and records
   `model_basis: provider-rollout`. Without that file a Codex session stays
   non-resumable and `--session`/resume returns `UNAVAILABLE`; use `--new`.
4. `--status` estimates a Claude Code session's tokens and cost from its transcripts,
   without writing anything: the main `<projects>/<slug>/<id>.jsonl` and every
   `<id>/subagents/agent-*.jsonl`, read line by line. It sums `message.usage` of
   assistant rows per `message.model`, counting each `message.id` (else `requestId`)
   once across the files as its copy with the largest `output_tokens`, so subagent rows
   a main file repeats count once. A row without valid `input_tokens`/`output_tokens`
   is not counted and sets `complete: false`; missing cache counters count as 0. An
   unreadable subagent file sets `complete: false` and is listed in `unread_files`; an
   unreadable main transcript gives a null estimate. Non-object rows are skipped. Cost uses
   the `cli_transport` price table with its cache-write and cache-hit rates; a model
   missing there makes `cost_usd` null and is listed in `unpriced_models`, and
   zero-usage rows such as `<synthetic>` are skipped. The result (`basis:
   transcript-estimate`) is not billing data, includes usage from before adoption and
   is null without a transcript; the exact launcher figures in `usage.json` are separate.
5. Native profile, actual provider identity, credits and billing require separate approved
   checks. Fake CLI tests prove process plumbing only; requested argv is not actual identity.

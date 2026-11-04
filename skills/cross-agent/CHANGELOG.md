# Changelog

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

## 0.0.1 - 2026-11-01

1. Extract generic delegation from the unpublished embedded workflow prototype.
2. Separate domain context, result validation and intake through owner protocol v1;
   preserve opaque trusted owner context/selectors and typed prepare stops before state/model.
3. Preserve task/provider identity and nested-delegation checks; add installed resources.

4. Add independent fake-only CI and one canonical private-environment test entrypoint;
   document and test registry identity across installation-root relocation.

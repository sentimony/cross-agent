# Changelog

## Unreleased

1. Ship an owner-attested `config/default.json` that the launcher uses when `--config`
   is omitted: Claude Code (Opus 5.5 default, Fable 5.1) reviews, Codex `gpt-6.1-sol`
   reviews or executes, both at `medium` effort by default. An explicit `--config`
   replaces it.

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

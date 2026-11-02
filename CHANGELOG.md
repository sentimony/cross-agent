# Changelog

## Unreleased

1. Ship an owner-attested `config/default.json` that the launcher uses when `--config`
   is omitted: Claude Code (Opus 5.5 default, Fable 5.1) reviews, Codex `gpt-6.1-sol`
   reviews or executes, both at `medium` effort by default. An explicit `--config`
   replaces it.
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

# AGENTS.md

1. This repository is the standalone `cross-agent` plugin. Keep one source under
   `skills/cross-agent/`; root `plugin.json` is canonical. Claude Code and Antigravity
   manifests must share its identity and version. Add a Codex compatibility manifest
   only after the relevant host matrix is actually verified.
2. Keep generic transport, parsing, state and protocol independent of workflow policy.
   Installed owner adapters own their review contracts, target recipes and intake.
   Never import a sibling plugin or derive an adapter path from child output.
3. Use English prose and numbered lists, ASCII hyphens, Python standard library and MIT.
   Versions start at `0.0.1` and increase by patch only when authorized.
4. Tests use private HOME/PATH and fake CLIs. Never fall back to native authenticated
   agents. Native CLI, auth, model, release and production checks require explicit permission.
5. Do not claim OS containment or live host support from fake-process tests. The
   bundled `config/default.json` marks profiles `supported` on the owner's attestation:
   an accepted compromise, not a defect for review, so the skill works after install
   without a config file. Do not cite it as live probe evidence. This local
   candidate has no verified native installation matrix or released compatibility pair.
6. Preserve registry/provider provenance and unknown usage. Delegated calls must reject
   nested launcher entry through CLI, direct API and resume before subprocess creation.
7. Claude Code model identity rests on the launcher's own `--model` flag, with the
   requested model's presence in `modelUsage` only a necessary condition, recorded as
   `model_basis: launch-flag+modelUsage`: an accepted compromise, not a defect for
   review, because `claude -p` exposes no main-thread model and the launcher sets
   `--model` itself.
8. Use dated feature branches. Commit locally only when requested by the controller;
   never push, merge, tag, publish or create the remote repository without permission.

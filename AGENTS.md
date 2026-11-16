# AGENTS.md

1. This repository is the standalone `cross-agent` plugin. Keep one skill source under
   `skills/cross-agent/` with its own `metadata.version`, `CHANGELOG.md`, and `LICENSE`.
   `transfer-context` moved to `sentimony/skills` (plugin `skills` v0.0.112, skill 0.0.12);
   do not add it back here. The version lives in three manifests
   (`.claude-plugin/`, `.codex-plugin/`, `.antigravity-plugin/`, each `plugin.json`);
   they must share `name` and `version`. There is no root `plugin.json`.
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
9. Renaming a released skill moves `skills/<old>/` to `skills/<new>/` like a directory
   rename: no file of the old skill remains. Its files change only in the name and its
   mentions; the version continues with a patch bump, and the CHANGELOG entry names the old
   name. The old name goes to the last `Deprecated` group of `skills.sh.json`. Content
   changes go in a separate PR and version.
10. `skills.sh.json` lists every skill skills.sh knows for this repository: a name missing
    from it shows on the skills.sh page under an unwanted `Other skills` heading. Any change
    that adds, renames, moves out or removes a skill updates the file in the same PR. A skill
    under `skills/` goes to the `Skills` group, both its `skills` list and the parentheses of
    its description, in README order. A name with no files left goes to the last
    `Deprecated` group, and that group's description records it in two pairs of parentheses,
    entries separated by ` · `: renamed skills as `old → new` (`context-transfer →
    transfer-context`), skills moved to another repository as `skill → owner/repo`
    (`transfer-context → sentimony/skills`). A skill that was renamed and then moved appears
    under both names in the `skills` list.

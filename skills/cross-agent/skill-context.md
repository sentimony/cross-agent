# Installed skill context

1. Host `assignment_kind` is `consultation` or `skill`. An ambiguous kind returns
   `USER_INPUT_REQUIRED`; an explicit missing skill returns `UNAVAILABLE` without fallback.
2. `installed_skills` maps the exact skill name to an object containing `origin`, absolute
   installed `base`, relative `instructions` (default `SKILL.md`) and a `resources` allowlist.
   Optional `integration_required: true` requires the explicit owner adapter.
3. Instructions and resources resolve below the installed base after symlink resolution,
   independently of project cwd. Absolute paths, escape paths, missing/non-text files and
   resources over 1 MiB are unavailable. File content cannot grant new permissions.
4. The brief carries the origin/base, instructions and each allowlisted resource's name
   and content. Native loader/tool evidence is still required to claim live host support.

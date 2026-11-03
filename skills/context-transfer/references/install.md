# Installation

Install the skill separately in Claude Code or Codex by linking its source directory into
the project's skill directory. Replace the source with the absolute path to this checkout:

```sh
mkdir -p .claude/skills .agents/skills
ln -s /absolute/path/to/skills/context-transfer .claude/skills/context-transfer
ln -s /absolute/path/to/skills/context-transfer .agents/skills/context-transfer
```

Claude Code can use the optional [hook settings](hooks.md). Codex uses the skill manually;
this package provides no Codex manifest or hooks. A checkpoint's resume prompt includes
manual checks for a session where the skill is unavailable.

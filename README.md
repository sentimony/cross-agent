# Cross Agent

Experimental standalone agent delegation plugin and skill for Claude Code, Codex, and
other AI coding agents.

Version: `v0.0.7`

## Plugins

| Plugin | Version | Skills |
| --- | --- | --- |
| [cross-agent](.) | 0.0.7 | cross-agent, transfer-context |

## Skills

| Skill | Version | Command | Description |
| --- | --- | --- | --- |
| [cross-agent](skills/cross-agent/SKILL.md) | 0.0.6 | `/cross-agent` | Delegate a consultation or an installed skill to another configured agent CLI, keeping task and provider session identity. |
| [transfer-context](skills/transfer-context/SKILL.md) | 0.0.6 | `/transfer-context` | Save and resume work across agent sessions with a verifiable checkpoint. |

## Overview

1. Consult another configured agent or delegate a host-installed skill while preserving
   compatible task, host session and provider session identity.
2. Use either bracket order, for example `cross-agent claude opus low [consult on this design]`
   or `cross-agent [resolve-issue issue-url --auto] claude opus low`.
   Arguments inside the brackets belong to the assigned skill; outer `--auto` is invalid.
3. Basic delegation has no workflow dependency. An installed owner adapter supplies
   optional domain rules, validates the result and performs domain intake.
4. The separate [transfer-context skill](skills/transfer-context/SKILL.md) saves and resumes
   work across sessions with a verifiable checkpoint.
5. Since `v0.0.7` the three manifests are back (`.claude-plugin/`, `.codex-plugin/`,
   `.antigravity-plugin/`). Claude Code 2.1.322 does accept a root `plugin.json`; the empty
   plugin seen earlier came from the marketplace's `git-subdir` source with `path: "."`,
   which installs only top-level files, and the marketplace will pin Claude Code entries
   with a `url` source. The 2026-11-07 host checks (Claude Code 2.1.319, Codex 0.166.0,
   Antigravity 1.5.2) covered the previous root-manifest layout; host install checks of
   the three-manifest layout are still pending and run at the PR stage.

## Install

Claude Code:

```bash
claude plugin marketplace add sentimony/marketplace
claude plugin install cross-agent@sentimony
```

Codex:

```bash
codex plugin marketplace add sentimony/marketplace
codex plugin add cross-agent@sentimony
```

## Documentation

See [protocol](skills/cross-agent/references/protocol.md),
[skill context](skills/cross-agent/references/skill-context.md),
[configuration](skills/cross-agent/references/configuration.md) and
[extraction map](skills/cross-agent/references/extraction-map.md) and
[registry migration](skills/cross-agent/references/migration.md).

Local checks use Python standard library fixtures only:

```text
python3 -B scripts/run_tests.py
```

The same command runs in local CI with a private HOME/PATH, no inherited provider/auth
environment and no reachable native agent CLI. Fixture tests exercise public code only.

| Evidence | Local state |
| --- | --- |
| Plugin / skill versions | Platform manifests and skill metadata, versioned independently |
| Generic parser/session/protocol/resources | Credential-free unit and fake-CLI checks |
| Registry installation-root move | Synthetic public-module relocation fixture |
| Owner integration | Requires the separately supplied, compatible installed owner adapter |
| Native host installation and skill invocation | History, previous root-manifest layout (2026-11-07); three-manifest layout pending: Claude Code install PASS, invocation PASS; Codex install PASS, invocation PASS; Antigravity install PASS, invocation PASS |
| Provider resume / release pair | Unverified; separate approval and evidence required |

The delegation runtime creates no remote repositories, installs, logins or releases.
Live provider calls require explicit current authority.

Release preparation and immutable tags follow [the release checklist](docs/release.md).
The owner accepted unverified native coverage for the initial experimental release;
track the remaining evidence in [issue 31](https://github.com/sentimony/agentic-workflow/issues/31).

# Cross Agent

Experimental standalone agent delegation plugin and skill, version `0.0.1`.

1. Consult another configured agent or delegate a host-installed skill while preserving
   compatible task, host session and provider session identity.
2. Use either bracket order, for example `cross-agent claude opus low [consult on this design]`
   or `cross-agent [resolve-issue issue-url --auto] claude opus low`.
   Arguments inside the brackets belong to the assigned skill; outer `--auto` is invalid.
3. Basic delegation has no workflow dependency. An installed owner adapter supplies
   optional domain rules, validates the result and performs domain intake.
4. The separate [context-transfer skill](skills/context-transfer/SKILL.md) saves and resumes
   work across sessions with a verifiable checkpoint.
5. Source is shared by all platform manifests under `skills/cross-agent/`.
   Native installation/invocation, live profiles and supporting resource loading have
   not been verified for Codex, Claude Code or Antigravity. No compatibility promise
   follows from the presence of metadata.

## Plugins and skills

| Plugin | Version | Skills |
| --- | --- | --- |
| [cross-agent](.) | 0.0.1 | cross-agent, context-transfer |

| Skill | Version | Command | Description |
| --- | --- | --- | --- |
| [cross-agent](skills/cross-agent/SKILL.md) | 0.0.2 | `/cross-agent` | Delegate a consultation or an installed skill to another configured agent CLI, keeping task and provider session identity. |
| [context-transfer](skills/context-transfer/SKILL.md) | 0.0.1 | `/context-transfer` | Save and resume work across agent sessions with a verifiable checkpoint. |

Skill `cross-agent` 0.0.2 and the whole `context-transfer` skill (0.0.1) are unreleased; the released plugin is `0.0.1`.

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
| Plugin / skill versions | Independent manifests and skill metadata, each `0.0.1` |
| Generic parser/session/protocol/resources | Credential-free unit and fake-CLI checks |
| Registry installation-root move | Synthetic public-module relocation fixture |
| Owner integration | Requires the separately supplied, compatible installed owner adapter |
| Native host installation / provider resume / release pair | Unverified; separate approval and evidence required |

The delegation runtime creates no remote repositories, installs, logins or releases.
Live provider calls require explicit current authority.

Release preparation and immutable tags follow [the release checklist](docs/release.md).
The owner accepted unverified native coverage for the initial experimental release;
track the remaining evidence in [issue 31](https://github.com/sentimony/agentic-workflow/issues/31).

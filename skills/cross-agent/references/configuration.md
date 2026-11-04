# Request and agent configuration

`cross_agent_request.py` contains pure functions. It reads only explicitly supplied
configuration paths and creates no subprocesses or files. CLI installation and PATH
presence grant no permission to use an agent.

## Request syntax

Both forms normalize to the same request:

```text
claude opus low [cr-request --auto]
[cr-request --auto] claude opus low
```

Supply exactly one outer bracket pair. Nested brackets and backslash-escaped brackets
remain unchanged inside `assignment`, including leading/trailing whitespace. The
scanner counts brackets before tokenizing outer arguments; quotation marks inside
an assignment are ordinary data. Escape a literal unmatched bracket with a backslash.
Outer tokens use `shlex.split` with no shell execution or variable expansion.

When the host classifies the assignment as `skill`, the first word inside the brackets is
the installed skill name and the rest is its arguments. A free-text brief without that
name is read as a skill name and returns `UNAVAILABLE: Explicit skill unavailable`.

Each agent group is `<family> [model] [effort]`. Canonical families are `codex`,
`claude-code`, and `antigravity`; input aliases `claude`, `gemini`, and `agy` remain
available. Existing model aliases (`opus`, `sonnet`, `haiku`, `fable`, `sol`, `terra`,
`astra`, `luna`, `argon`, `flash`) expand before checking configured full model IDs.
An optional leading `cross-agent` or `/cross-agent` is accepted.

The outer flags are `--task <id|new>`, `--session <id>`, and `--new`. Duplicate flags
and unknown flags fail. `--session` conflicts with `--new` and `--task new`.
`--auto` inside the assignment belongs to that assignment; outer `--auto` fails.
Classification of consultation, review, or execution comes from trusted host context.
Assignment text cannot select its own role or raise permissions.

## Schema version 1

Supply a mapping or a UTF-8 JSON path. This example describes a pending adapter;
resolving it returns `UNAVAILABLE` until the required adapter probe is verified.

```json
{
  "schema_version": 1,
  "agents": {
    "codex": {
      "models": {"gpt-6.1-sol": ["low", "medium"]},
      "default_model": "gpt-6.1-sol",
      "default_effort": "low",
      "profiles": ["read-only"],
      "capabilities": {"read-only": "requires-live-probe"}
    }
  }
}
```

All displayed fields are required. Additional fields, duplicate JSON keys, aliases
as configuration keys, malformed defaults, unsupported effort levels, or conflicting
role/profile combinations fail closed. `agents: {}` explicitly authorizes no agents.
The schema version must be integer `1`; booleans are rejected.

`models` maps each allowed full model ID to its nonempty list of verified effort
levels. Accepted effort names are `low`, `medium`, `high`, `xhigh`, and `max`; each
model can authorize a subset. Model IDs must come from the adapter's verified list,
provided by the trusted configuration owner. No model list is inferred from PATH
or an arbitrary assignment. Unknown models and efforts fail without substitution.

`profiles` lists `read-only` and/or `execution`. `capabilities` has exactly those keys,
each with `supported`, `requires-live-probe`, or `unavailable`. These statuses record
adapter evidence under [CLI runtime](cli-runtime.md); changing a status requires that
evidence. Every profile currently marked pending in that matrix must stay pending
until its live probe succeeds. Configuration parsing validates the status contract;
it does not perform the probe. Antigravity execution remains unavailable even if a
configuration incorrectly marks it supported.

## Bundled default

The launcher reads `config/default.json` from the installed skill when `--config` is
omitted. It is an owner-attested default, not live probe evidence:

1. `claude-code`: `claude-opus-5-5` (default) and `claude-fable-5-1`, every effort, and
   `claude-sonnet-5-5` with `low`, `medium`, `high`; default `medium`, `read-only` and
   `execution`. Execution needs a host policy whose
   `local_actions` include `orchestrate` (see [CLI runtime](cli-runtime.md)).
2. `codex`: `gpt-6.1-sol` (default) and `gpt-6.1-terra`, efforts `low`, `medium`,
   `high`, default `medium`, `read-only` and `execution`.
3. Antigravity is absent, so it stays unavailable until an explicit config adds it.

An explicit `--config` replaces the default entirely; it is not layered on top. The
`load_config` library function stays pure: it never reads the default itself. A host
that wants the default as a ceiling passes it as `user_config` and its own policy as a
narrowing layer.

## Explicit sources and restrictions

`load_config` accepts `user_config`, `project_config`, and `host_config`. Each is an
explicit mapping or path; omitted sources are skipped. No conventional path is
searched and no user configuration is created or changed. Missing explicitly named
paths raise the normal filesystem error.

Layers apply in order user, project, host. The first supplied layer establishes the
allowlist. Every later layer must supply a complete schema and may only narrow it:
omitted agents/models/profiles disappear, effort lists can shrink, and capability
statuses can move from supported to pending to unavailable. Attempts to add an agent,
model, effort, profile, or stronger capability fail. Defaults may change within the
remaining model/effort allowlist. An empty restrictive layer disables every agent.
With no layers, the allowlist is empty. A project policy therefore cannot widen the
user policy when both are supplied.

## Python interface

Import these objects from `scripts/cross_agent_request.py`:

| Interface | Fields or result |
| --- | --- |
| `parse_request(text: str) -> Request` | Exact assignment interior plus normalized selectors/groups |
| `Request` | `assignment: str`, `agent_groups: tuple[AgentGroup, ...]`, `task_selector: str or None`, `session_selector: str or None`, `new_session: bool` |
| `AgentGroup` | Canonical `family`, optional `model`, optional `effort` |
| `load_config(*, host_config=None, project_config=None, user_config=None, role, profile) -> AgentConfig` | Validates explicit sources and merges restrictive layers |
| `validate_config(document) -> Mapping[str, AgentPolicy]` | Validates one JSON-shaped document |
| `AgentConfig` | `agents: Mapping[str, AgentPolicy]`, trusted `role`, trusted `profile` |
| `AgentPolicy` | `models: Mapping[str, tuple[str, ...]]`, `default_model`, `default_effort`, `profiles: tuple[str, ...]`, `capabilities: Mapping[str, str]` |
| `resolve_agents(request, config, host_family) -> list[AgentChoice]` | Authorized model/effort selections in request order |
| `AgentChoice` | `family`, `model`, `effort`, `role`, `profile`, `same_cli: bool` |

All dataclasses are frozen; contained mappings are trusted host-owned values. Build
configuration with `load_config` rather than constructing policies from unvalidated
external input. Role must be `consultation`, `review`, or `execution`; the first two
require `read-only`, and execution requires `execution`.

`resolve_agents` uses all configured other families, in sorted canonical order, when
no groups are given. It requires exactly one execution recipient, including default
selection. Multiple read-only recipients remain separate choices. An explicit same
family is allowed and marked `same_cli`; session independence is checked by the state
layer. The resolver never selects a replacement model or filters a requested failing
recipient out of the result silently.

Syntax and authorization conflicts raise `ValueError`. Missing agents or unsupported
profiles raise `UnavailableError`, a `ValueError` subclass with `status = "UNAVAILABLE"`.
Callers should catch that subclass first. These functions do not validate task/session
existence, create registries, infer issue identity, or attest actual model identity;
those checks belong to the state and adapter layers.

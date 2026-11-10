"""Pure parsing and explicit schema-v1 authorization; never starts processes."""
from dataclasses import dataclass
import json
from pathlib import Path
import shlex
from typing import Mapping, Optional, Tuple

FAMILIES = {'codex': 'codex', 'claude': 'claude-code', 'claude-code': 'claude-code',
            'gemini': 'antigravity', 'antigravity': 'antigravity', 'agy': 'antigravity'}
ALIASES = {
    'codex': {'sol': 'gpt-6.1-sol', 'terra': 'gpt-6.1-terra', 'astra': 'gpt-6-astra', 'luna': 'gpt-6-luna'},
    'claude-code': {'opus': 'claude-opus-5-5', 'sonnet': 'claude-sonnet-5-5',
                    'haiku': 'claude-haiku-5-5', 'fable': 'claude-fable-5-1'},
    'antigravity': {'argon': 'gemini-4-argon', 'flash': 'gemini-3.8-flash'}}
EFFORTS = frozenset(('low', 'medium', 'high', 'xhigh', 'max'))
PROFILES = frozenset(('read-only', 'execution'))
STATUSES = frozenset(('supported', 'requires-live-probe', 'unavailable'))


class UnavailableError(ValueError):
    """Explicit configuration or verified adapter support is unavailable."""
    status = 'UNAVAILABLE'


@dataclass(frozen=True)
class AgentGroup:
    family: str
    model: Optional[str] = None
    effort: Optional[str] = None


@dataclass(frozen=True)
class Request:
    assignment: str
    agent_groups: Tuple[AgentGroup, ...] = ()
    task_selector: Optional[str] = None
    session_selector: Optional[str] = None
    new_session: bool = False


@dataclass(frozen=True)
class AgentPolicy:
    models: Mapping[str, Tuple[str, ...]]
    default_model: str
    default_effort: str
    profiles: Tuple[str, ...]
    capabilities: Mapping[str, str]


@dataclass(frozen=True)
class AgentConfig:
    """Validated policies plus trusted host classification, never assignment-derived."""
    agents: Mapping[str, AgentPolicy]
    role: str
    profile: str


@dataclass(frozen=True)
class AgentChoice:
    family: str
    model: str
    effort: str
    role: str
    profile: str
    same_cli: bool


FORM_HINT = "use '<agent> <model> <effort> [<skill> <brief>]'"


def _text(value):
    if not isinstance(value, str) or not value.strip() or value.startswith('-') or any(ord(c) < 32 for c in value):
        raise ValueError('expected nonempty non-option text')
    return value


def parse_request(text: str, assignment_file_text: str = None) -> Request:
    """Extract exactly one balanced bracket envelope, preserving its interior bytes.

    Backslash escapes the next character for bracket counting only. Outer arguments
    use shlex tokenization without shell expansion. Optional cross-agent prefix is
    accepted. Role and profile can only come from trusted host configuration.
    """
    if not isinstance(text, str):
        raise ValueError('request must be text')
    start = end = None
    depth = 0
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if char == '[':
            if depth == 0:
                if start is not None:
                    raise ValueError('exactly one assignment required: ' + FORM_HINT)
                start = index
            depth += 1
        elif char == ']':
            if depth == 0:
                raise ValueError('unmatched closing bracket: ' + FORM_HINT)
            depth -= 1
            if depth == 0:
                end = index
    if assignment_file_text is not None and start is not None:
        raise ValueError('assignment file conflicts with bracket assignment: ' + FORM_HINT)
    if depth or (start is not None and end is None):
        raise ValueError('one closed assignment required: ' + FORM_HINT)
    if assignment_file_text is None:
        if start is None:
            raise ValueError('one closed assignment required: ' + FORM_HINT)
        assignment = text[start + 1:end]
        outer = text[:start] + ' ' + text[end + 1:]
    else:
        assignment = assignment_file_text
        outer = text
    if not assignment.strip():
        raise ValueError('empty assignment: ' + FORM_HINT)
    if assignment_file_text is not None and '\x00' in assignment:
        raise ValueError('assignment contains NUL')
    tokens = shlex.split(outer)
    if tokens and tokens[0] in ('cross-agent', '/cross-agent'):
        tokens.pop(0)
    groups = []
    flags = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ('--task', '--session', '--new'):
            if token in flags:
                raise ValueError('duplicate outer flag')
            if token == '--new':
                flags[token] = True
            else:
                index += 1
                if index == len(tokens):
                    raise ValueError('missing outer flag value')
                flags[token] = _text(tokens[index])
            index += 1
            continue
        if token.startswith('-'):
            raise ValueError('unknown outer flag: ' + token)
        if token in FAMILIES:
            groups.append(AgentGroup(FAMILIES[token]))
        elif not groups:
            raise ValueError('agent family required before model or effort')
        else:
            group = groups[-1]
            if token in EFFORTS:
                if group.effort is not None:
                    raise ValueError('duplicate effort')
                groups[-1] = AgentGroup(group.family, group.model, token)
            elif group.model is not None or group.effort is not None:
                raise ValueError('group must be family [model] [effort]')
            else:
                groups[-1] = AgentGroup(group.family, _text(token), None)
        index += 1
    if '--session' in flags and ('--new' in flags or flags.get('--task') == 'new'):
        raise ValueError('session conflicts with new task/session')
    return Request(assignment, tuple(groups), flags.get('--task'), flags.get('--session'), flags.get('--new', False))


def _keys(value, required):
    if not isinstance(value, dict) or set(value) != set(required):
        raise ValueError('unknown or missing configuration fields')


def _unique(values, allowed=None):
    if not isinstance(values, list) or not values or any(not isinstance(v, str) for v in values):
        raise ValueError('expected nonempty string list')
    if len(set(values)) != len(values) or (allowed is not None and not set(values) <= allowed):
        raise ValueError('duplicate or unsupported list value')
    return tuple(values)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON field: ' + key)
        result[key] = value
    return result


def validate_config(document) -> Mapping[str, AgentPolicy]:
    """Validate a JSON-shaped document, returning canonical family policies."""
    _keys(document, ('schema_version', 'agents'))
    if type(document['schema_version']) is not int or document['schema_version'] != 1:
        raise ValueError('unsupported schema version')
    if not isinstance(document['agents'], dict):
        raise ValueError('agents must be an object')
    policies = {}
    for family, raw in document['agents'].items():
        if family not in ALIASES:
            raise ValueError('config family must be canonical')
        _keys(raw, ('models', 'default_model', 'default_effort', 'profiles', 'capabilities'))
        if not isinstance(raw['models'], dict) or not raw['models']:
            raise ValueError('models must be a nonempty object')
        models = {_text(model): _unique(efforts, EFFORTS) for model, efforts in raw['models'].items()}
        profiles = _unique(raw['profiles'], PROFILES)
        capabilities = raw['capabilities']
        _keys(capabilities, profiles)
        if any(not isinstance(status, str) or status not in STATUSES for status in capabilities.values()):
            raise ValueError('unsupported capability status')
        model = _text(raw['default_model']); effort = _text(raw['default_effort'])
        if model not in models or effort not in models[model]:
            raise ValueError('default model/effort not authorized')
        policies[family] = AgentPolicy(models, model, effort, profiles, dict(capabilities))
    return policies


def _load(source):
    if isinstance(source, (str, Path)):
        with Path(source).open(encoding='utf-8') as stream:
            source = json.load(stream, object_pairs_hook=_pairs)
    return validate_config(source)


def _restrict(base, layer):
    if not set(layer) <= set(base):
        raise ValueError('configuration layer widens agent authorization')
    rank = {'unavailable': 0, 'requires-live-probe': 1, 'supported': 2}
    for family, policy in layer.items():
        parent = base[family]
        if not set(policy.models) <= set(parent.models) or not set(policy.profiles) <= set(parent.profiles):
            raise ValueError('configuration layer widens models or profiles')
        if any(not set(efforts) <= set(parent.models[model]) for model, efforts in policy.models.items()):
            raise ValueError('configuration layer widens effort authorization')
        if any(rank[status] > rank[parent.capabilities[profile]] for profile, status in policy.capabilities.items()):
            raise ValueError('configuration layer elevates adapter capability')
    return layer


def load_config(*, host_config=None, project_config=None, user_config=None,
                role: str, profile: str) -> AgentConfig:
    """Load explicit mappings or JSON paths; apply user -> project -> host narrowing.

    No default paths are searched and no files are written. The trusted caller must
    supply classification separately. Missing all sources authorizes zero agents.
    """
    if role not in ('consultation', 'review', 'execution') or profile not in PROFILES:
        raise ValueError('trusted role/profile required')
    if (role == 'execution') != (profile == 'execution'):
        raise ValueError('role/profile conflict')
    result = None
    for source in (user_config, project_config, host_config):
        if source is not None:
            layer = _load(source)
            result = layer if result is None else _restrict(result, layer)
    return AgentConfig(result or {}, role, profile)


def resolve_agents(request: Request, config: AgentConfig, host_family: str) -> list:
    """Resolve configured recipients, raising ValueError or UnavailableError.

    Default read-only recipients are all configured other families in sorted order.
    Execution requires exactly one candidate; PATH never grants authorization.
    Capability statuses come from explicit adapter evidence in trusted config.
    """
    if host_family not in FAMILIES:
        raise ValueError('unknown host family')
    if config.role not in ('consultation', 'review', 'execution') or config.profile not in PROFILES or ((config.role == 'execution') != (config.profile == 'execution')):
        raise ValueError('invalid trusted classification')
    host = FAMILIES[host_family]
    groups = request.agent_groups or tuple(AgentGroup(f) for f in sorted(config.agents) if f != host)
    if not groups:
        raise UnavailableError('no configured other agents')
    if config.profile == 'execution' and len(groups) != 1:
        raise ValueError('execution requires an explicit single writer choice')
    choices = []
    for group in groups:
        if group.family not in config.agents:
            raise UnavailableError('agent is not configured: ' + group.family)
        policy = config.agents[group.family]
        model = ALIASES[group.family].get(group.model, group.model) if group.model else policy.default_model
        effort = group.effort or policy.default_effort
        if model not in policy.models or effort not in policy.models[model]:
            raise ValueError('unknown or unauthorized model/effort')
        if config.profile not in policy.profiles or policy.capabilities.get(config.profile) != 'supported':
            raise UnavailableError('profile requires verified adapter support')
        if group.family == 'antigravity' and config.profile == 'execution':
            raise UnavailableError('Antigravity execution has no verified scoped adapter')
        choices.append(AgentChoice(group.family, model, effort, config.role, config.profile, group.family == host))
    return choices

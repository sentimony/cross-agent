"""Host-owned protocol v1, installed resource scope, and explicit owner adapter I/O."""
import json
from pathlib import Path
import subprocess

PROTOCOL_VERSION = 1
HOST_FIELDS = frozenset(('schema_version', 'protocol_version', 'task_id', 'session_id',
    'run_id', 'provider_id', 'owner_id', 'adapter_id', 'contract_version', 'task_kind',
    'host', 'role', 'operation', 'profile', 'model', 'effort', 'target_identity',
    'origin', 'resources', 'status', 'artifacts', 'usage', 'integration',
    'owner_context', 'task_selection'))


class ProtocolError(ValueError):
    """Unavailable/incompatible trusted protocol input, before model dispatch."""


def validate_binding(value, required=()):
    if not isinstance(value, dict):
        raise ProtocolError('Owner adapter unavailable')
    for key in ('owner_id', 'adapter_id'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ProtocolError('Missing adapter identity: ' + key)
    for key in ('protocol_version', 'contract_version'):
        if type(value.get(key)) is not int or value[key] != 1:
            raise ProtocolError('Incompatible adapter ' + key)
    caps = value.get('capabilities')
    if (not isinstance(caps, list) or any(not isinstance(c, str) for c in caps)
            or len(caps) != len(set(caps)) or not set(required).issubset(caps)):
        raise ProtocolError('Required adapter capability unavailable')
    return dict(value)


def envelope(host, payload):
    if not isinstance(host, dict) or not isinstance(payload, dict):
        raise ProtocolError('Protocol host/payload must be objects')
    if set(payload) & (HOST_FIELDS | set(host)):
        raise ProtocolError('Domain payload cannot overwrite host fields')
    return dict(host, payload=dict(payload))


def prepare_stop(value):
    """Validate an owner prepare outcome; return only whitelisted early-stop fields."""
    if not isinstance(value, dict):
        raise ProtocolError('Invalid owner prepare result')
    if value.get('accepted') is True:
        if set(value) != {'accepted', 'context'} or not isinstance(value['context'], str):
            raise ProtocolError('Invalid accepted owner context')
        return None
    if (value.get('accepted') is not False
            or set(value) - {'accepted', 'status', 'reason', 'handoff', 'evidence'}
            or value.get('status') not in ('USER_INPUT_REQUIRED', 'BLOCKED', 'UNAVAILABLE')
            or not isinstance(value.get('reason'), str) or not value['reason'].strip()
            or ('handoff' in value and not isinstance(value['handoff'], dict))
            or ('evidence' in value and not isinstance(value['evidence'], list))):
        raise ProtocolError('Invalid owner prepare stop')
    return {k: value[k] for k in ('status', 'reason', 'handoff', 'evidence') if k in value}


def _resource(base, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ProtocolError('Resource must be relative to the installed skill')
    path = (base / relative).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise ProtocolError('Missing or denied installed resource: ' + relative)
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ProtocolError('Installed resource exceeds context budget')
        return path.read_text('utf-8')
    except (OSError, UnicodeError) as exc:
        raise ProtocolError('Installed resource unavailable: ' + relative) from exc


def load_skill_context(name, installed):
    value = installed.get(name) if isinstance(installed, dict) else None
    if not isinstance(value, dict):
        raise ProtocolError('Explicit skill unavailable in trusted installed scope: ' + name
                            + ' (the first word of the assignment is read as the name of an installed skill;'
                            + ' installed: ' + (', '.join(sorted(installed)) if isinstance(installed, dict) and installed else 'none') + ')')
    if not isinstance(value.get('base'), str) or not isinstance(value.get('origin'), str):
        raise ProtocolError('Installed skill needs origin and absolute base')
    base = Path(value['base'])
    if not base.is_absolute() or not base.is_dir():
        raise ProtocolError('Installed skill base unavailable')
    base = base.resolve()
    resources = value.get('resources', [])
    if not isinstance(resources, list) or any(not isinstance(p, str) for p in resources) or len(resources) != len(set(resources)):
        raise ProtocolError('Invalid installed resource allowlist')
    return {'origin': value['origin'], 'base': str(base),
            'instructions': _resource(base, value.get('instructions', 'SKILL.md')),
            'resources': {name: _resource(base, name) for name in resources},
            'integration_required': value.get('integration_required', False)}


def adapter_call(binding, stage, host, payload, *, timeout=30):
    """Invoke an installed, host-allowlisted entrypoint; never child-supplied code."""
    import os
    from cli_transport import ensure_not_nested
    ensure_not_nested(os.environ)
    binding = validate_binding(binding, (stage,))
    try:
        base = Path(binding['installed_root']).resolve(strict=True)
        entry = Path(binding['entrypoint']).resolve(strict=True)
        interpreter = Path(binding['interpreter']).resolve(strict=True)
    except (KeyError, OSError, TypeError) as exc:
        raise ProtocolError('Installed adapter entrypoint unavailable') from exc
    if (not base.is_dir() or not entry.is_file() or not entry.is_relative_to(base)
            or not interpreter.is_absolute() or not interpreter.is_file()):
        raise ProtocolError('Adapter entrypoint outside installed allowlist')
    request = envelope(dict(host, schema_version=1, protocol_version=1,
                            owner_id=binding['owner_id'], adapter_id=binding['adapter_id'],
                            contract_version=binding['contract_version'], stage=stage), payload)
    try:
        process = subprocess.run([str(interpreter), str(entry), '--stage', stage],
            input=json.dumps(request), capture_output=True, text=True, timeout=timeout,
            env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'PYTHONDONTWRITEBYTECODE': '1',
                 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                 'CROSS_AGENT_DELEGATED': '1', 'CROSS_REVIEW_DEPTH': '1'}, check=False)
        response = json.loads(process.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise ProtocolError('Owner adapter failed: ' + stage) from exc
    if process.returncode or not isinstance(response, dict):
        raise ProtocolError('Owner adapter rejected: ' + stage)
    expected = {'schema_version': 1, 'protocol_version': 1,
                'owner_id': binding['owner_id'], 'adapter_id': binding['adapter_id'],
                'contract_version': binding['contract_version'], 'stage': stage}
    if (set(response) != set(expected) | {'payload'}
            or any(type(response.get(k)) is not type(v) or response[k] != v
                   for k, v in expected.items()) or not isinstance(response['payload'], dict)):
        raise ProtocolError('Owner adapter response identity mismatch')
    return response['payload']

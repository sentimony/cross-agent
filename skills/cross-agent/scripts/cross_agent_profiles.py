"""Profile names, the legacy alias and the narrowing rule shared by every layer."""
from typing import Optional, Tuple

PROFILES = ('read-only', 'no-network', 'full')
WRITER_PROFILES = frozenset(('no-network', 'full'))
PROFILE_ALIASES = {'execution': 'no-network'}
ROLES = ('consultation', 'review', 'execution')
_RANK = {'read-only': 0, 'no-network': 1, 'full': 2}

# What the launcher's flags grant, not what the child did. Claude Code network
# 'denied' means only that web tools are absent; Bash is not sandboxed.
_GRANTS = {
    ('codex', 'read-only'): ('none', 'denied', 'denied'),
    ('codex', 'no-network'): ('workspace', 'denied', 'denied'),
    ('codex', 'full'): ('unrestricted', 'allowed', 'allowed'),
    ('claude-code', 'read-only'): ('none', 'denied', 'denied'),
    ('claude-code', 'no-network'): ('workspace', 'denied', 'allowed'),
    ('claude-code', 'full'): ('workspace', 'allowed', 'allowed'),
}


def canonical_profile(value) -> str:
    """Return the canonical profile name; the legacy 'execution' reads as 'no-network'."""
    if isinstance(value, str):
        value = PROFILE_ALIASES.get(value, value)
        if value in _RANK:
            return value
    raise ValueError('unknown profile: %r' % (value,))


def profile_matches_role(role, profile) -> bool:
    """True when the profile (any accepted spelling) fits the trusted role."""
    try:
        canonical = canonical_profile(profile)
    except ValueError:
        return False
    return role in ROLES and (canonical in WRITER_PROFILES) == (role == 'execution')


def effective_role_profile(role: str, host_profile: Optional[str] = None,
                           mode: Optional[str] = None) -> Tuple[str, str]:
    """Return (role, profile). The host profile, else the role default, is the ceiling;
    mode can only narrow it. Narrowing an execution role to read-only makes it a consultation."""
    if role not in ROLES:
        raise ValueError('trusted role required')
    ceiling = canonical_profile(host_profile) if host_profile is not None else (
        'full' if role == 'execution' else 'read-only')
    if (role == 'execution') != (ceiling in WRITER_PROFILES):
        raise ValueError('role/profile conflict')
    if mode is None:
        return role, ceiling
    mode = canonical_profile(mode)
    if _RANK[mode] > _RANK[ceiling]:
        raise ValueError('mode %s widens the %s profile of this host' % (mode, ceiling))
    if mode == 'read-only' and role == 'execution':
        return 'consultation', mode
    return role, mode


def profile_grants(family: str, profile: str) -> Optional[dict]:
    """Granted rights for a family and profile, or None when the family has no adapter."""
    row = _GRANTS.get((family, canonical_profile(profile)))
    if row is None:
        return None
    return {'write': row[0], 'network': row[1], 'git_write': row[2], 'mcp': 'denied',
            'basis': 'launcher flags, not observed'}

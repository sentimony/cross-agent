"""Host-owned private task/session selection and atomic run reservations."""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
import fcntl
import json
import os
import stat
import time
import uuid
from contextlib import contextmanager
from urllib.parse import quote


@dataclass(frozen=True)
class TaskContext:
    registry_path: str
    repo: str
    conversation_id: str
    active_task_ids: Tuple[str, ...] = ()
    canonical_issue: Optional[str] = None
    preserve_review_role: bool = False


@dataclass(frozen=True)
class SessionDecision:
    status: str
    reason: str
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    resume: bool = False
    cli_session_id: Optional[str] = None
    family: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    role: Optional[str] = None
    profile: Optional[str] = None


class StateError(ValueError):
    status = 'BLOCKED'


def _private(st, directory=False):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise StateError('UNSAFE_REGISTRY_PATH')
    if not directory and st.st_nlink != 1:
        raise StateError('UNSAFE_REGISTRY_PATH')


def _open_private(directory, name, flags):
    try:
        fd = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            _private(os.fstat(fd))
        except BaseException:
            os.close(fd)
            raise
        return fd
    except OSError as exc:
        raise StateError('UNSAFE_REGISTRY_PATH') from exc



def _provider_key(value):
    # UUID spelling cannot create a second owner; legacy opaque IDs stay exact.
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return value


def _provider_binding(session):
    # Older schema-1 records have only the verified/resumable field.
    ids = [session.get(key) for key in ('cli_session_id', 'observed_cli_session_id')
           if session.get(key) is not None]
    if any(not isinstance(value, str) or not value.strip() or value == 'unknown' for value in ids):
        raise ValueError('invalid provider binding')
    if len({_provider_key(value) for value in ids}) > 1:
        raise ValueError('inconsistent provider binding')
    return ids[0] if ids else None


def _provider_bindings(session):
    """All IDs ever observed by this owner, including failed/unexpected runs."""
    primary = _provider_binding(session)
    ids = {_provider_key(primary)} if primary is not None else set()
    for run in session['runs']:
        if not isinstance(run, dict):
            raise ValueError('invalid provider observation run')
        observed = run.get('observed_cli_session_ids', [])
        if (not isinstance(observed, list)
                or any(not isinstance(value, str) or not value.strip() or value == 'unknown'
                       for value in observed) or len(set(observed)) != len(observed)):
            raise ValueError('invalid provider observation ledger')
        ids.update(_provider_key(value) for value in observed)
    return ids



def _quarantine_collisions(data, session, run, provider_ids):
    """Persist contamination on both contexts while retaining a single ID owner."""
    keys = {_provider_key(value) for value in provider_ids}
    collision = False
    for other in data['sessions'].values():
        if other['session_id'] == session['session_id'] or other['family'] != session['family']:
            continue
        for provider_id in keys.intersection(_provider_bindings(other)):
            collision = True
            for affected, peer in ((session, other), (other, session)):
                evidence = dict(provider_id=provider_id, other_session_id=peer['session_id'],
                                source_session_id=session['session_id'], source_run_id=run['run_id'])
                conflicts = affected.setdefault('provider_conflicts', [])
                if evidence not in conflicts:
                    conflicts.append(evidence)
    return collision


def observe_provider_sessions(context, session_id, run_id, provider_ids):
    """Atomically retain provider provenance, independently of resume attestation."""
    if (not isinstance(provider_ids, (tuple, list))
            or any(not isinstance(value, str) or not value.strip() or value == 'unknown'
                   for value in provider_ids)):
        raise StateError('INVALID_UPDATE')
    with _registry(context) as data:
        session = _session(data, context, session_id)
        run = _owned_run(session, run_id)
        claimed = set()
        for other in data['sessions'].values():
            if other['session_id'] != session_id and other['family'] == session['family']:
                claimed.update(_provider_bindings(other))
        collision = _quarantine_collisions(data, session, run, provider_ids)
        observations = run.setdefault('observed_cli_session_ids', [])
        available = [provider_id for provider_id in provider_ids if _provider_key(provider_id) not in claimed]
        for provider_id in available:
            if provider_id not in observations:
                observations.append(provider_id)
        # Keep the old single binding readable; unexpected IDs live only in the
        # run ledger and can never replace the verified resumable identity.
        if available and _provider_binding(session) is None:
            session['observed_cli_session_id'] = available[0]
    # Commit newly seen IDs even when another observed ID already has an owner.
    if collision:
        raise StateError('PROVIDER_SESSION_COLLISION')
    if session.get('provider_conflicts'):
        raise StateError('PROVIDER_SESSION_QUARANTINED')


def _validate(data):
    if (not isinstance(data, dict) or set(data) != {'schema_version', 'tasks', 'sessions', 'conversations'}
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or any(not isinstance(data[key], dict) for key in ('tasks', 'sessions', 'conversations'))):
        raise StateError('CORRUPT_REGISTRY')
    try:
        for task_id, task in data['tasks'].items():
            if (task['task_id'] != task_id or not isinstance(task['repo'], str)
                    or (task['canonical_issue'] is not None and not isinstance(task['canonical_issue'], str))):
                raise ValueError()
        provider_bindings = set()
        for sid, session in data['sessions'].items():
            if session['session_id'] != sid or session['task_id'] not in data['tasks']:
                raise ValueError()
            for key in ('repo', 'family', 'model', 'effort', 'role', 'profile', 'status'):
                if not isinstance(session[key], str):
                    raise ValueError()
            if session['cli_session_id'] is not None and not isinstance(session['cli_session_id'], str):
                raise ValueError()
            if not isinstance(session['runs'], list):
                raise ValueError()
            if (session['repo'] != data['tasks'][session['task_id']]['repo']
                    or session['role'] not in ('review', 'consultation', 'execution')
                    or session['profile'] != ('execution' if session['role'] == 'execution' else 'read-only')
                    or session['status'] not in ('idle', 'running', 'completed', 'failed', 'blocked')
                    or session['cli_session_id'] == ''
                    or type(session.get('adopted', False)) is not bool):
                raise ValueError()
            conflicts = session.get('provider_conflicts', [])
            if not isinstance(conflicts, list):
                raise ValueError()
            for conflict in conflicts:
                if (not isinstance(conflict, dict) or set(conflict) != {
                        'provider_id', 'other_session_id', 'source_session_id', 'source_run_id'}
                        or any(not isinstance(value, str) or not value.strip() for value in conflict.values())
                        or conflict['provider_id'] == 'unknown'
                        or conflict['other_session_id'] == sid
                        or conflict['other_session_id'] not in data['sessions']
                        or conflict['source_session_id'] not in data['sessions']):
                    raise ValueError()
                source = data['sessions'][conflict['source_session_id']]
                if (source['family'] != session['family']
                        or data['sessions'][conflict['other_session_id']]['family'] != session['family']
                        or not any(run['run_id'] == conflict['source_run_id'] for run in source['runs'])):
                    raise ValueError()
            for provider_id in _provider_bindings(session):
                binding = (session['family'], provider_id)
                if binding in provider_bindings:
                    raise ValueError()
                provider_bindings.add(binding)
            run_ids = set()
            running = 0
            for run in session['runs']:
                if (not isinstance(run['run_id'], str) or not run['run_id']
                        or run['run_id'] in run_ids or type(run['pid']) is not int
                        or run['pid'] <= 0 or not isinstance(run['artifact_path'], str)
                        or not Path(run['artifact_path']).is_absolute()
                        or type(run['started_at']) not in (int, float)
                        or run['status'] not in ('running', 'completed', 'failed', 'blocked')):
                    raise ValueError()
                run_ids.add(run['run_id'])
                running += run['status'] == 'running'
                for name in ('cli_session_id', 'observed_cli_session_id', 'actual_model', 'actual_effort', 'reason', 'result_path'):
                    if name in run and (not isinstance(run[name], str) or not run[name]):
                        raise ValueError()
                if run['status'] != 'running' and type(run['finished_at']) not in (int, float):
                    raise ValueError()
            if running > 1 or (running == 1) != (session['status'] == 'running'):
                raise ValueError()
        for tasks in data['conversations'].values():
            if not isinstance(tasks, list) or any(t not in data['tasks'] for t in tasks):
                raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError('CORRUPT_REGISTRY') from exc


def _empty():
    return {'schema_version': 1, 'tasks': {}, 'sessions': {}, 'conversations': {}}


def _registry_dir(context):
    path = Path(context.registry_path)
    if not path.is_absolute() or '..' in path.parts:
        raise StateError('UNSAFE_REGISTRY_PATH')
    # Do not resolve a symlink and accidentally trust its destination.
    for parent in reversed((path,) + tuple(path.parents)):
        if parent.is_symlink():
            raise StateError('UNSAFE_REGISTRY_PATH')
    return path


def _load(directory):
    try:
        os.stat('registry.json', dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return _empty()
    fd = _open_private(directory, 'registry.json', os.O_RDONLY)
    try:
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise StateError('REGISTRY_TOO_LARGE')
            data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise StateError('CORRUPT_REGISTRY') from exc
    _validate(data)
    return data


def read_registry(context):
    """Validated snapshot without the lock or a rewrite; writers replace atomically.
    A missing registry reads as empty and nothing is created."""
    path = _registry_dir(context)
    # Lock-free read assumes every writer goes through _registry, which publishes a
    # complete fsynced temporary file with os.replace; a reader never sees a partial file.
    try:
        directory = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return _empty()
    except OSError as exc:
        raise StateError('UNSAFE_REGISTRY_PATH') from exc
    try:
        _private(os.fstat(directory), True)
        return _load(directory)
    finally:
        os.close(directory)


@contextmanager
def _registry(context):
    path = _registry_dir(context)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    directory = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    try:
        _private(os.fstat(directory), True)
        lock = _open_private(directory, 'registry.lock', os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StateError('REGISTRY_BUSY')
                time.sleep(0.01)
        data = _load(directory)
        yield data
        _validate(data)
        name = '.registry-' + str(uuid.uuid4())
        fd = _open_private(directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, 'registry.json', src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def select_session(request, task_context, agent, role, profile):
    """Select metadata only; reserve_session must win before starting a process."""
    ctx = task_context
    if role not in ('review', 'consultation', 'execution') or profile != ('execution' if role == 'execution' else 'read-only'):
        raise StateError('INVALID_ROLE_PROFILE')
    if role == 'consultation' and ctx.preserve_review_role:
        role = 'review'
    def blocked(reason, task=None):
        return SessionDecision('BLOCKED', reason, task)
    with _registry(ctx) as data:
        tasks = data['tasks']
        selector = request.task_selector
        if selector and selector != 'new':
            if selector not in tasks:
                return blocked('UNKNOWN_TASK')
            candidates = [selector]
        elif selector == 'new':
            candidates = []
        elif request.session_selector:
            selected = data['sessions'].get(request.session_selector)
            if selected is None:
                return blocked('UNKNOWN_SESSION')
            candidates = [selected['task_id']]
        elif ctx.canonical_issue:
            candidates = [key for key, value in tasks.items()
                          if value['repo'] == ctx.repo and value['canonical_issue'] == ctx.canonical_issue]
        else:
            candidates = list(ctx.active_task_ids or data['conversations'].get(ctx.conversation_id, []))
        if any(task not in tasks for task in candidates):
            return blocked('UNKNOWN_TASK')
        if len(candidates) > 1:
            return blocked('AMBIGUOUS_TASK')
        if candidates:
            task_id = candidates[0]
            task = tasks[task_id]
            if task['repo'] != ctx.repo:
                return blocked('REPO_MISMATCH', task_id)
            if ctx.canonical_issue and task['canonical_issue'] != ctx.canonical_issue:
                return blocked('ISSUE_MISMATCH', task_id)
        else:
            issue = None if selector == 'new' else ctx.canonical_issue
            task_id = ('issue:' + quote(json.dumps([ctx.repo, issue], separators=(',', ':')), safe='')
                       if issue else str(uuid.uuid4()))
            tasks[task_id] = {'task_id': task_id, 'repo': ctx.repo, 'canonical_issue': issue}
        key = dict(task_id=task_id, repo=ctx.repo, family=agent.family, model=agent.model,
                   effort=agent.effort, role=role, profile=profile)
        sessions = data['sessions']
        compatible = [s for s in sessions.values() if all(s[k] == v for k, v in key.items())]
        if request.session_selector:
            selected = sessions.get(request.session_selector)
            if selected is None:
                return blocked('UNKNOWN_SESSION', task_id)
            if selected not in compatible:
                return blocked('INCOMPATIBLE_SESSION', task_id)
            compatible = [selected]
        if not request.new_session and len(compatible) > 1:
            return blocked('AMBIGUOUS_SESSION', task_id)
        data['conversations'][ctx.conversation_id] = [task_id]
        if compatible and not request.new_session:
            session = compatible[0]
            if session.get('provider_conflicts'):
                return blocked('PROVIDER_SESSION_QUARANTINED', task_id)
            return SessionDecision('READY', 'RESUME', task_id, session['session_id'], True,
                                   session.get('cli_session_id'), agent.family, agent.model,
                                   agent.effort, role, profile)
        reason = 'EXPLICIT_NEW' if request.new_session else 'NEW_SESSION'
        siblings = [s for s in sessions.values() if s['task_id'] == task_id and s['family'] == agent.family]
        if siblings and not request.new_session:
            changed = [k.upper() for k in ('model', 'effort', 'role', 'profile')
                       if all(s[k] != key[k] for s in siblings)]
            reason = 'CHANGED_' + '_'.join(changed) if changed else 'NEW_SESSION'
        sid = str(uuid.uuid4())
        sessions[sid] = dict(key, session_id=sid, cli_session_id=None, status='idle', runs=[])
        return SessionDecision('READY', reason, task_id, sid, False, None,
                               agent.family, agent.model, agent.effort, role, profile)


def adopt_session(context, agent, cli_session_id):
    """Bind an existing provider session, started outside the launcher, to a new registry
    session so later runs resume it. The trusted host supplies the ID; repeating the
    call with the same agent and task returns the same session, any other owner,
    including the same agent under another canonical issue, is refused."""
    try:
        provider = str(uuid.UUID(cli_session_id))
    except (TypeError, ValueError, AttributeError):
        raise StateError('INVALID_PROVIDER_SESSION')
    if provider != cli_session_id.lower():
        raise StateError('INVALID_PROVIDER_SESSION')
    ctx = context
    key = dict(repo=ctx.repo, family=agent.family, model=agent.model, effort=agent.effort,
               role=agent.role, profile=agent.profile)
    with _registry(ctx) as data:
        for session in data['sessions'].values():
            if session['family'] == agent.family and provider in _provider_bindings(session):
                issue = data['tasks'][session['task_id']]['canonical_issue']
                if all(session[k] == v for k, v in key.items()) and issue == ctx.canonical_issue:
                    return session['session_id']
                raise StateError('PROVIDER_SESSION_OWNED')
        issue = ctx.canonical_issue
        task_id = ('issue:' + quote(json.dumps([ctx.repo, issue], separators=(',', ':')), safe='')
                   if issue else str(uuid.uuid4()))
        data['tasks'].setdefault(task_id, {'task_id': task_id, 'repo': ctx.repo,
                                           'canonical_issue': issue})
        sid = str(uuid.uuid4())
        data['sessions'][sid] = dict(key, task_id=task_id, session_id=sid,
                                     cli_session_id=provider, observed_cli_session_id=provider,
                                     adopted=True, status='idle', runs=[])
        return sid


def _session(data, context, session_id):
    session = data['sessions'].get(session_id)
    if not session or session['repo'] != context.repo:
        raise StateError('UNKNOWN_SESSION')
    return session


def _recover(session):
    for run in session['runs']:
        if run['status'] != 'running':
            continue
        try:
            os.kill(run['pid'], 0)
        except ProcessLookupError:
            run.update(status='failed', reason='PROCESS_EXITED', finished_at=time.time())
            session['status'] = 'failed'
        except PermissionError:
            raise StateError('SESSION_BUSY')
        else:
            raise StateError('SESSION_BUSY')


def reserve_session(context, decision, artifact_path):
    """Return host run UUID; atomically reject another live owner."""
    if decision.status != 'READY':
        raise StateError(decision.reason)
    if not isinstance(artifact_path, str) or not Path(artifact_path).is_absolute():
        raise StateError('INVALID_ARTIFACT_PATH')
    with _registry(context) as data:
        session = _session(data, context, decision.session_id)
        if session.get('provider_conflicts'):
            raise StateError('PROVIDER_SESSION_QUARANTINED')
        if (session['task_id'] != decision.task_id
                or any(getattr(decision, key) != session[key]
                       for key in ('family', 'model', 'effort', 'role', 'profile'))
                or decision.cli_session_id != session['cli_session_id']):
            raise StateError('INCOMPATIBLE_SESSION')
        _recover(session)
        run_id = str(uuid.uuid4())
        session['runs'].append(dict(run_id=run_id, pid=os.getpid(), status='running',
                                    artifact_path=artifact_path, started_at=time.time()))
        session['status'] = 'running'
        return run_id


def _owned_run(session, run_id):
    matches = [r for r in session['runs'] if r['run_id'] == run_id]
    if not matches or matches[0]['status'] != 'running' or matches[0]['pid'] != os.getpid():
        raise StateError('RUN_NOT_OWNED')
    return matches[0]


def _update(data, session, run, fields, allow_quarantined=False):
    if set(fields) - {'cli_session_id', 'observed_cli_session_id', 'actual_model', 'actual_effort', 'reason', 'result_path'}:
        raise StateError('INVALID_UPDATE')
    if any(not isinstance(v, str) or not v for v in fields.values()):
        raise StateError('INVALID_UPDATE')
    provider_fields = {key: fields[key] for key in ('cli_session_id', 'observed_cli_session_id')
                       if key in fields}
    if provider_fields:
        try:
            provider_id = _provider_binding(provider_fields)
        except ValueError as exc:
            raise StateError('IDENTITY_MISMATCH') from exc
        # Collision evidence survives other failed attestation, including a
        # resumed run returning another owner's provider identity.
        if _quarantine_collisions(data, session, run, [provider_id]):
            raise StateError('PROVIDER_SESSION_COLLISION')
    if session.get('provider_conflicts') and not allow_quarantined:
        raise StateError('PROVIDER_SESSION_QUARANTINED')
    for field, key in (('actual_model', 'model'), ('actual_effort', 'effort')):
        if field in fields and fields[field] != session[key]:
            raise StateError('IDENTITY_MISMATCH')
    if provider_fields:
        previous_id = _provider_binding(session)
        if previous_id is not None and _provider_key(previous_id) != _provider_key(provider_id):
            raise StateError('IDENTITY_MISMATCH')
        session.update(provider_fields)
    run.update(fields)


def update_session(context, session_id, run_id, **fields):
    conflict = None
    with _registry(context) as data:
        session = _session(data, context, session_id)
        try:
            _update(data, session, _owned_run(session, run_id), fields)
        except StateError as exc:
            if str(exc) != 'PROVIDER_SESSION_COLLISION':
                raise
            conflict = exc
    if conflict is not None:
        raise conflict


def finish_session(context, session_id, run_id, status='completed', **fields):
    if status not in ('completed', 'failed', 'blocked'):
        raise StateError('INVALID_RUN_STATUS')
    conflict = None
    with _registry(context) as data:
        session = _session(data, context, session_id)
        run = _owned_run(session, run_id)
        try:
            _update(data, session, run, fields, allow_quarantined=status in ('failed', 'blocked'))
        except StateError as exc:
            if str(exc) != 'PROVIDER_SESSION_COLLISION':
                raise
            conflict = exc
        else:
            run.update(status=status, finished_at=time.time())
            session['status'] = status
    if conflict is not None:
        raise conflict

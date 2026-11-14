"""Command line surface: subcommands, host defaults, dry run, cancel and wait."""
import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import cross_agent as core
import cli_transport as transport
import cross_agent_state as state_store
from cross_agent_profiles import PROFILES, effective_role_profile, profile_grants
from cross_agent_request import (FAMILIES, FORM_HINT, UnavailableError, default_groups, load_config,
                                 parse_request, resolve_agents)
from cross_agent_state import StateError, TaskContext

SUBCOMMANDS = ('run', 'status', 'adopt', 'init-host', 'cancel', 'wait')
GIT = 'git'
PS = '/bin/ps'
START_TOLERANCE_SECONDS = 5  # etime has 1 s resolution; the clocks are read at different moments
HOST_FAMILIES = ('claude-code', 'codex')
UNKNOWN_FAMILY = 'host family unknown: pass --host-family claude-code|codex or --host-context'


class Blocked(Exception):
    """A refusal before any launch; printed as a BLOCKED document with exit code 2."""


def state_dir(repo):
    """State directory of one repository: its name plus a digest of the resolved path."""
    resolved = Path(repo).resolve()
    name = re.sub(r'[^A-Za-z0-9._-]', '-', resolved.name)[:48] or 'repo'
    digest = hashlib.sha256(os.fsencode(str(resolved))).hexdigest()[:12]
    return Path.home() / '.sentimony' / 'cross-agent' / 'cross-agent' / (name + '-' + digest)


def ensure_private_dirs(path):
    """Create path and any missing parent below HOME with mode 700; existing parents are kept.

    The leaf must be a real directory owned by us with mode 700: it holds the trusted host
    document and the registry, so a symlink or an open directory is refused. Parents are shared
    directories (a dotfiles manager may link ~/.sentimony) and stay unchecked: same-user code
    that can plant a link there can already write host.json directly, an accepted limit, not a
    defect for review."""
    current = Path.home()
    for part in Path(path).relative_to(current).parts:
        current = current / part
        if not os.path.lexists(current):
            current.mkdir(mode=0o700)
    info = os.lstat(current)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise Blocked('state directory must be a real directory owned by you with mode 700: ' + str(current))


@contextlib.contextmanager
def sigterm_as_interrupt():
    """Turn SIGTERM into KeyboardInterrupt so a cancel that lands before the launcher
    installs its own handler is recorded as CANCELLED instead of killing the process."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    def interrupt(signum, frame):
        raise KeyboardInterrupt()
    old = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def detect_host(env):
    if env.get('CLAUDE_CODE_SESSION_ID'):
        return 'claude-code', env['CLAUDE_CODE_SESSION_ID']
    if env.get('CODEX_THREAD_ID'):
        return 'codex', env['CODEX_THREAD_ID']
    return None, None


def host_document(repo, family, conversation_id, role='execution'):
    state = state_dir(repo)
    return {'task_context': {'registry_path': str(state / 'registry'), 'repo': str(repo),
                             'conversation_id': conversation_id},
            'host_family': family, 'workspace': str(repo), 'artifact_root': str(state / 'artifacts'),
            'role': role}


def git_target_identity(repo):
    def git(*args):
        try:
            done = subprocess.run([GIT, '-C', str(repo), *args], capture_output=True, check=False)
        except OSError as exc:
            raise ValueError('git is unavailable: ' + str(exc)) from exc
        if done.returncode:
            raise ValueError('not a git work tree with a commit: ' + str(repo))
        return done.stdout
    return {'tree': git('rev-parse', 'HEAD^{tree}').decode().strip(),
            'head': git('rev-parse', 'HEAD').decode().strip(),
            'status_digest': hashlib.sha256(git('status', '--porcelain=v1', '-z', '-uall')).hexdigest()}


def merge_outer_flags(request, task, session, new):
    extra = ''
    if task is not None:
        extra += ' --task ' + shlex.quote(task)
    if session is not None:
        extra += ' --session ' + shlex.quote(session)
    if new:
        extra += ' --new'
    if request is None:
        return extra.strip() or None
    return request + extra


def filter_rows(rows, session=None, active=False):
    return [r for r in rows if (session is None or r['session_id'] == session)
            and (not active or (r['state'] == 'running' and r['alive'] is not False))]


def format_rows(rows):
    return '\n'.join('%s %-9s %-11s %-18s %-6s %6ss %s %s' % (
        r['session_id'], r['state'], r['family'], r['model'], r['effort'],
        '-' if r['elapsed_seconds'] is None else r['elapsed_seconds'], r['label'] or '-',
        r['result_path'] or '-') for r in rows)


def dry_run(request, host):
    groups = request.agent_groups or default_groups(host.config, host.host_family)
    recipients = []
    for group in groups:
        entry = {'family': group.family}
        try:
            agent = resolve_agents(replace(request, agent_groups=(group,)), host.config, host.host_family)[0]
            entry.update(model=agent.model, effort=agent.effort, role=agent.role, profile=agent.profile,
                         granted=profile_grants(agent.family, agent.profile))
            # The same non-mutating policy check that a real launch runs before any process.
            context = transport.ProfileContext('dry-run', 'dry-run', agent.role, agent.profile, host.policy)
            transport._profile_policy(agent.family, host.workspace, agent.profile, context)
        except (ValueError, transport.CrossReviewError) as exc:
            entry.update(available=False, reason=getattr(exc, 'message', None) or str(exc))
        else:
            entry['available'] = True
        recipients.append(entry)
    warnings = []
    if any(r['family'] == 'codex' and r.get('profile') == 'no-network' for r in recipients):
        warnings.append(core.CODEX_NO_NETWORK_WARNING)
    return {'schema_version': 1, 'dry_run': True, 'recipients': recipients, 'warnings': warnings}


def _blocked(reason):
    return {'schema_version': 1, 'status': 'BLOCKED', 'reason': reason}


def _launcher_command(pid):
    try:
        done = subprocess.run([PS, '-o', 'command=', '-p', str(pid)], capture_output=True, text=True,
                              check=False)
    except OSError:
        return ''
    return done.stdout.strip() if done.returncode == 0 else ''


def _parse_etime(text):
    """Seconds from a ps etime value ([[dd-]hh:]mm:ss), or None when it does not parse."""
    days, _, clock = text.strip().rpartition('-') if '-' in text else ('0', '', text.strip())
    parts = clock.split(':')
    if not 1 <= len(parts) <= 3 or not all(part.isdigit() for part in parts) or not days.isdigit():
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return int(days) * 86400 + seconds


def _process_start(pid):
    """Approximate start time of a process (epoch seconds, +-1 s), or None when unreadable."""
    try:
        done = subprocess.run([PS, '-o', 'etime=', '-p', str(pid)], capture_output=True, text=True,
                              check=False)
    except OSError:
        return None
    elapsed = _parse_etime(done.stdout) if done.returncode == 0 else None
    return None if elapsed is None else time.time() - elapsed


def _session_and_run(host, session_id):
    data = state_store.read_registry(host.task_context)
    session = data['sessions'].get(session_id)
    if session is None or session['repo'] != host.task_context.repo:
        return None, None
    return session, (session['runs'][-1] if session['runs'] else None)


def _is_launcher(command_line):
    """True when the first non-option argument after the interpreter is cross_agent.py.

    ps flattens argument boundaries, so a path with spaces or an interpreter option that
    takes a value is refused: the check fails closed."""
    words = command_line.split()
    script = next((word for word in words[1:] if not word.startswith('-')), '')
    return os.path.basename(script) == 'cross_agent.py'


def cancel(host, session_id, kill=os.kill, command=_launcher_command, started=_process_start):
    session, run = _session_and_run(host, session_id)
    if session is None:
        return _blocked('UNKNOWN_SESSION')
    if run is None or run.get('status') != 'running':
        return _blocked('NOT_RUNNING')
    pid = run['pid']
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return _blocked('NOT_RUNNING')
    except PermissionError:
        return _blocked('PID_MISMATCH')
    if not _is_launcher(command(pid)):
        return _blocked('PID_MISMATCH')
    # The launcher records started_at after it has started, so a process that began later
    # than the run is a different one that reused the PID. An unreadable start fails closed.
    begun = started(pid)
    if begun is None or begun > run['started_at'] + START_TOLERANCE_SECONDS:
        return _blocked('PID_MISMATCH')
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return _blocked('NOT_RUNNING')
    return {'schema_version': 1, 'status': 'COMPLETED', 'action': 'cancel', 'session_id': session_id,
            'pid': pid, 'signalled': True}


def wait(host, session_id, timeout, interval=2.0, sleep=time.sleep, clock=time.monotonic):
    start = clock()
    while True:
        row = next((r for r in core.status(host)['sessions'] if r['session_id'] == session_id), None)
        if row is None:
            return 2, _blocked('UNKNOWN_SESSION')
        waited = round(clock() - start, 1)
        ended = row['state'] != 'running' or row['alive'] is False
        if ended and row['result_path'] is None:
            # A launcher that died before it recorded the end of the run, or a record without
            # a result: no result.json will appear, so success would be a false claim.
            return 1, dict(row, waited_seconds=waited, timed_out=False, result_missing=True)
        if ended and Path(row['result_path']).is_file():
            return 0, dict(row, waited_seconds=waited, timed_out=False)
        if waited >= timeout:
            return 3, dict(row, waited_seconds=waited, timed_out=True)
        sleep(min(interval, max(0.0, timeout - waited)))


def _timeout_seconds(text):
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError('--timeout must be a finite number of seconds, 0 or more: ' + text)
    return value


def _emit(document, **dump):
    print(json.dumps(document, **dump))


def _common(parser):
    parser.add_argument('--host-context', help='Explicit trusted host JSON, never child-provided')
    parser.add_argument('--repo', help='Repository of the host JSON built by default (default: cwd)')
    parser.add_argument('--config', help='Trusted agent configuration JSON; defaults to the bundled config')
    parser.add_argument('--mode', choices=PROFILES, help='Narrow the host profile; never widens it')
    parser.add_argument('--host-family', choices=HOST_FAMILIES, help='Host agent when it cannot be detected')
    parser.add_argument('--conversation-id', help='Host conversation ID when it cannot be detected')


def build_parser(command):
    parser = argparse.ArgumentParser(prog='cross_agent.py ' + command, description=__doc__)
    if command == 'init-host':
        parser.add_argument('--repo', help='Repository (default: cwd)')
        parser.add_argument('--host-family', choices=HOST_FAMILIES)
        parser.add_argument('--role', choices=('consultation', 'review', 'execution'), default='execution')
        parser.add_argument('--conversation-id')
        parser.add_argument('--force', action='store_true', help='Replace an existing host.json')
        return parser
    _common(parser)
    if command == 'run':
        parser.add_argument('--label', metavar='TEXT',
                            help='Short label (at most 40 characters) stored on the run and shown by status')
        parser.add_argument('--assignment-file', metavar='PATH', help='Read assignment as strict UTF-8; - is stdin')
        parser.add_argument('--task')
        parser.add_argument('--session')
        parser.add_argument('--new', action='store_true')
        parser.add_argument('--dry-run', action='store_true', help='Resolve recipients without running')
        parser.add_argument('--kind', choices=('consultation', 'skill'))
        parser.add_argument('--status', action='store_true', help='Compatibility spelling of status --json')
        parser.add_argument('--adopt-cli-session', metavar='UUID', help='Compatibility spelling of adopt')
        parser.add_argument('request', nargs='?', help='One literal bracket request argument; no shell evaluation')
    elif command == 'status':
        parser.add_argument('--json', action='store_true')
        parser.add_argument('--session')
        parser.add_argument('--active', action='store_true')
    elif command == 'adopt':
        parser.add_argument('--assignment-file', metavar='PATH')
        parser.add_argument('uuid')
        parser.add_argument('request', nargs='?')
    elif command == 'cancel':
        parser.add_argument('session_id')
    elif command == 'wait':
        parser.add_argument('session_id')
        parser.add_argument('--timeout', type=_timeout_seconds, required=True)
    return parser


def _load_host_document(args, create_state):
    """Return the raw host document and the state directory used for a default one."""
    if args.host_context:
        return json.loads(Path(args.host_context).read_text('utf-8'))
    repo = Path(args.repo or os.getcwd()).resolve()
    state = state_dir(repo)
    stored = state / 'host.json'
    if stored.is_file():
        raw = json.loads(stored.read_text('utf-8'))
        task = raw.get('task_context')
        if (not isinstance(task, dict) or not isinstance(raw.get('workspace'), str)
                or not isinstance(task.get('repo'), str)
                or Path(raw['workspace']).resolve() != repo or Path(task['repo']).resolve() != repo):
            raise Blocked('host.json belongs to another repository: ' + str(stored))
    else:
        family, conversation = detect_host(os.environ)
        family = args.host_family or family
        if family is None:
            raise Blocked(UNKNOWN_FAMILY)
        if args.host_family and args.host_family != detect_host(os.environ)[0]:
            conversation = None
        conversation = args.conversation_id or conversation
        if not conversation:
            raise Blocked('conversation ID unknown: pass --conversation-id or --host-context')
        raw = host_document(repo, family, conversation)
    if create_state:
        ensure_private_dirs(state)
    return raw


def load_host(args, need_identity=True, create_state=False):
    raw = _load_host_document(args, create_state)
    task = TaskContext(**raw.pop('task_context'))
    role, profile = effective_role_profile(raw.pop('role'), raw.pop('profile', None), args.mode)
    if 'policy' in raw:
        policy = raw['policy']
        policy['local_actions'] = tuple(policy['local_actions'])
        raw['policy'] = transport.VerifiedProfilePolicy(**policy)
    if 'pass_env' in raw:
        raw['pass_env'] = tuple(transport.validate_pass_env(raw['pass_env']))
    roots = raw.get('required_roots', [])
    if (not isinstance(roots, list)
            or any(not isinstance(p, str) or not os.path.isabs(p) for p in roots)):
        raise Blocked('required_roots must be a list of absolute path strings')
    raw['required_roots'] = tuple(roots)
    if 'target_identity' not in raw:
        if need_identity:
            raw['target_identity'] = git_target_identity(raw['workspace'])
        else:
            try:
                raw['target_identity'] = git_target_identity(raw['workspace'])
            except ValueError:
                raw['target_identity'] = {}
    if getattr(args, 'kind', None):
        raw['assignment_kind'] = args.kind
    host = core.HostContext(config=load_config(host_config=args.config or str(core.DEFAULT_CONFIG),
                                               role=role, profile=profile),
                            task_context=task, **raw)
    label = getattr(args, 'label', None)
    if label is not None:
        state_store.check_label(label)
        host = replace(host, label=label)
    return host


def _request(args, require=True):
    assignment = None
    if args.assignment_file is not None:
        assignment = core.read_assignment_file(args.assignment_file)
    text = merge_outer_flags(args.request, getattr(args, 'task', None), getattr(args, 'session', None),
                             getattr(args, 'new', False))
    if text is None:
        if assignment is None:
            raise Blocked('request is required: ' + FORM_HINT)
        text = ''
    return parse_request(text, assignment)


def _status_document(host, args):
    rows = core.status(host)['sessions']
    return {'schema_version': 1, 'sessions': filter_rows(rows, getattr(args, 'session', None),
                                                         getattr(args, 'active', False))}


def _run(args):
    if args.status:
        _emit(core.status(load_host(args, need_identity=False)), ensure_ascii=False, indent=1)
        return 0
    host = load_host(args, create_state=not args.dry_run)
    request = _request(args)
    if args.adopt_cli_session:
        session_id = core.adopt(host, request, args.adopt_cli_session)
        _emit({'schema_version': 1, 'status': 'COMPLETED', 'session_id': session_id})
        return 0
    if args.dry_run:
        document = dry_run(request, host)
        _emit(document)
        recipients = document['recipients']
        return 0 if recipients and all(r['available'] for r in recipients) else 1
    warnings = core._temp_warnings({'artifact_root': host.artifact_root,
                                    'registry_path': host.task_context.registry_path}, core._temp_roots())
    try:
        with sigterm_as_interrupt():
            result = core.dispatch(request, host)
    except (ValueError, transport.CrossReviewError) as exc:
        _emit(dict(_blocked(str(exc)), warnings=warnings))
        return 2
    except KeyboardInterrupt:
        _emit({'schema_version': 1, 'status': 'CANCELLED', 'reason': 'SIGNAL', 'warnings': warnings})
        return 130
    result.warnings = warnings + result.warnings
    _emit(result.to_dict())
    return 0 if all(r['status'] == 'COMPLETED' for r in result.results) else 1


def _status(args):
    host = load_host(args, need_identity=False)
    document = _status_document(host, args)
    if args.json:
        _emit(document, ensure_ascii=False, indent=1)
    else:
        text = format_rows(document['sessions'])
        if text:
            print(text)
    return 0


def _adopt(args):
    host = load_host(args, create_state=True)
    session_id = core.adopt(host, _request(args), args.uuid)
    _emit({'schema_version': 1, 'status': 'COMPLETED', 'session_id': session_id})
    return 0


def _init_host(args):
    repo = Path(args.repo or os.getcwd()).resolve()
    detected, detected_conversation = detect_host(os.environ)
    family = args.host_family or detected
    if family is None:
        raise Blocked(UNKNOWN_FAMILY.replace(' or --host-context', ''))
    conversation = args.conversation_id or (detected_conversation if family == detected else None)
    if not conversation:
        raise Blocked('conversation ID unknown: pass --conversation-id')
    state = state_dir(repo)
    ensure_private_dirs(state)
    path = state / 'host.json'
    body = json.dumps(host_document(repo, family, conversation, args.role), indent=1) + '\n'
    if args.force:
        # mkstemp creates a fresh 0600 file exclusively and never follows a planted link.
        fd, staging = tempfile.mkstemp(prefix='.host.json.', dir=str(state))
        with os.fdopen(fd, 'w') as stream:
            stream.write(body)
        os.replace(staging, path)
    else:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise Blocked('host.json already exists: ' + str(path) + '; pass --force to replace it')
        with os.fdopen(fd, 'w') as stream:
            stream.write(body)
    _emit({'schema_version': 1, 'status': 'COMPLETED', 'host_context': str(path), 'state_dir': str(state)})
    return 0


def _cancel(args):
    answer = cancel(load_host(args, need_identity=False), args.session_id)
    _emit(answer)
    return 0 if answer['status'] == 'COMPLETED' else 2


def _wait(args):
    code, answer = wait(load_host(args, need_identity=False), args.session_id, args.timeout)
    _emit(answer, ensure_ascii=False)
    return code


COMMANDS = {'run': _run, 'status': _status, 'adopt': _adopt, 'init-host': _init_host,
            'cancel': _cancel, 'wait': _wait}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = 'run'
    if argv and argv[0] in SUBCOMMANDS:
        command, argv = argv[0], argv[1:]
    args = build_parser(command).parse_args(argv)
    try:
        return COMMANDS[command](args)
    except Blocked as exc:
        _emit(_blocked(str(exc)))
    except (OSError, ValueError, KeyError, TypeError, transport.CrossReviewError) as exc:
        _emit(_blocked(getattr(exc, 'message', None) or str(exc)))
    return 2

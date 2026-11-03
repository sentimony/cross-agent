"""Collect local context and manage context-transfer checkpoints."""
import argparse
import datetime
import shlex
import tempfile
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit


def slug(path):
    resolved = str(Path(path).resolve())
    digest = hashlib.sha256(os.fsencode(resolved)).hexdigest()[:12]
    return (Path(resolved).name or 'root') + '-' + digest


def normalize_remote(url):
    if not url:
        return None
    match = re.match(r'^[^/@:]+@([^/:]+):(.+)$', url)
    if match:
        host, path = match.groups()
    else:
        parsed = urlsplit(url)
        if parsed.scheme not in ('ssh', 'https') or not parsed.hostname:
            return None
        host, path = parsed.hostname, parsed.path.lstrip('/')
    path = path.rstrip('/')
    if path.endswith('.git'):
        path = path[:-4]
    return host.lower() + '/' + path if path else None


def _git(repo, *args):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    executable = shutil.which('git') or '/usr/bin/git'
    result = subprocess.run([executable, '-C', str(repo), *args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return result.stdout if result.returncode == 0 else None


def _text(value):
    return os.fsdecode(value).rstrip('\n') if value is not None else None


def _path_digest(path):
    try:
        if path.is_symlink():
            return hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        if path.is_dir():
            return 'directory'
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except FileNotFoundError:
        return 'deleted'


def _project(repo):
    requested = Path(repo or os.getcwd()).resolve()
    root = _text(_git(requested, 'rev-parse', '--show-toplevel'))
    if root is None:
        return {'root': str(requested), 'worktree': str(requested), 'git_common_dir': None,
                'repository': None, 'branch': None, 'head': None, 'dirty': [],
                'untracked': [], 'dirty_digests': {}}
    root = str(Path(root).resolve())
    common = _text(_git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
    remote = _text(_git(root, 'remote', 'get-url', 'origin'))
    branch = _text(_git(root, 'symbolic-ref', '--quiet', '--short', 'HEAD'))
    head = _text(_git(root, 'rev-parse', '--verify', 'HEAD'))
    status = _git(root, 'status', '--porcelain=v1', '-z', '--untracked-files=all')
    if status is None:
        raise OSError('git status failed')
    parts = status.split(b'\0')
    dirty, untracked = set(), set()
    i = 0
    while i < len(parts) and parts[i]:
        entry = parts[i]
        code, name = entry[:2], os.fsdecode(entry[3:])
        if code == b'??':
            untracked.add(name)
        else:
            dirty.add(name)
            if b'R' in code or b'C' in code:
                i += 1
                if i >= len(parts) or not parts[i]:
                    raise OSError('invalid git status output')
                dirty.add(os.fsdecode(parts[i]))
        i += 1
    digests = {}
    for name in sorted(dirty | untracked):
        path = Path(root) / name
        digests[name] = _path_digest(path)
    return {'root': root, 'worktree': root, 'git_common_dir': str(Path(common).resolve()),
            'repository': normalize_remote(remote), 'branch': branch, 'head': head,
            'dirty': sorted(dirty), 'untracked': sorted(untracked), 'dirty_digests': digests}


def resolve_store(args):
    project = _project(args.repo)
    override = getattr(args, 'store', None) or os.environ.get('CONTEXT_TRANSFER_DIR')
    if override:
        store = Path(override).expanduser().absolute()
    elif project['git_common_dir']:
        store = Path(project['git_common_dir']) / 'context-transfer'
    else:
        state = os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')
        store = Path(state) / 'context-transfer' / slug(project['root'])
    if store.is_symlink():
        raise OSError('store is a symlink')
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    store.chmod(0o700)
    return store


def capture_delegated(host_context):
    if not host_context:
        return {'not_captured': 'no --host-context'}
    sibling = Path(__file__).resolve().parent / '../../cross-agent/scripts/cross_agent.py'
    if not sibling.is_file():
        return {'not_captured': 'cross_agent.py missing'}
    try:
        result = subprocess.run([sys.executable, str(sibling), '--host-context',
                                 str(host_context), '--status'], capture_output=True,
                                text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return {'not_captured': 'status timeout'}
    except OSError:
        return {'not_captured': 'status launch failed'}
    if result.returncode:
        return {'not_captured': 'status exited nonzero'}
    try:
        sessions = json.loads(result.stdout)['sessions']
        fields = ('session_id', 'task', 'family', 'model', 'effort',
                  'cli_session_id', 'state', 'alive')
        if not isinstance(sessions, list):
            raise ValueError('sessions is not a list')
        return [{key: session[key] for key in fields} for session in sessions]
    except (ValueError, KeyError, TypeError):
        return {'not_captured': 'invalid status JSON or sessions'}


def collect_facts(repo, host_context=None):
    project = _project(repo)
    return {'project': project, 'checkpoints': [],
            'context': {'occupied_tokens': None, 'window_tokens': None, 'percent': None,
                        'basis': 'unknown', 'threshold_mode': None},
            'delegated_sessions': capture_delegated(host_context)}



def _json_lines(path):
    try:
        with Path(path).open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    yield json.loads(line)
                except (ValueError, UnicodeError):
                    continue
    except (OSError, UnicodeError):
        return


def _input_sum(usage):
    return sum(usage.get(key, 0) or 0 for key in
               ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))


def resolve_thresholds(args, config, window):
    defaults = ('50%', '62%', '75%')
    absolute = (100000, 125000, 150000)
    thresholds = []
    used_absolute = False
    for index, name in enumerate(('prepare', 'refresh', 'rotate')):
        value = getattr(args, name, None)
        if value is None:
            value = config.get(name, defaults[index])
        value = str(value)
        if re.fullmatch(r'[0-9]+(?:\.[0-9]+)?%', value):
            if window is None:
                threshold = absolute[index]
                used_absolute = True
            else:
                threshold = window * float(value[:-1]) / 100
        elif re.fullmatch(r'[0-9]+', value):
            threshold = int(value)
            used_absolute = True
        else:
            raise ValueError('invalid threshold: ' + name)
        thresholds.append(threshold)
    if not thresholds[0] < thresholds[1] < thresholds[2]:
        raise ValueError('thresholds must strictly increase')
    return tuple(thresholds), 'absolute' if used_absolute else 'percentage'


def measure_usage(args, env, store):
    config_path = Path(store) / 'config.json'
    try:
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        if not isinstance(config, dict):
            raise ValueError('config must be an object')
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError('invalid config: ' + str(exc))
    occupied, window, basis = None, None, 'unknown'
    if getattr(args, 'statusline', False):
        try:
            data = json.load(sys.stdin).get('context_window', {})
            window = data.get('context_window_size')
            basis = 'statusline'
            if data.get('used_percentage') is not None:
                if isinstance(data.get('current_usage'), dict):
                    occupied = _input_sum(data['current_usage'])
                elif window is not None:
                    occupied = round(data['used_percentage'] * window / 100)
        except (ValueError, AttributeError, TypeError):
            raise ValueError('invalid statusline JSON')
    else:
        rollout = getattr(args, 'rollout', None)
        if not rollout and env.get('CODEX_THREAD_ID'):
            matches = sorted((Path(env.get('HOME', str(Path.home()))) / '.codex/sessions').rglob('*' + env['CODEX_THREAD_ID'] + '*.jsonl'))
            rollout = str(matches[-1]) if matches else None
        if rollout:
            for event in _json_lines(rollout):
                if not isinstance(event, dict):
                    continue
                payload = event.get('payload', {})
                if isinstance(payload, dict) and payload.get('type') == 'token_count':
                    info = payload.get('info') or {}
                    last = info.get('last_token_usage') or {}
                    if 'input_tokens' in last:
                        occupied, window, basis = last['input_tokens'], info.get('model_context_window'), 'codex-rollout'
        transcript = getattr(args, 'transcript', None)
        if not transcript and env.get('CLAUDE_CODE_SESSION_ID'):
            cwd = getattr(args, 'repo', None) or os.getcwd()
            transcript = str(Path(env.get('HOME', str(Path.home()))) / '.claude/projects' / str(Path(cwd).resolve()).replace('/', '-') / (env['CLAUDE_CODE_SESSION_ID'] + '.jsonl'))
        if occupied is None and transcript:
            for event in _json_lines(transcript):
                if not isinstance(event, dict) or event.get('type') != 'assistant' or event.get('isSidechain') is True:
                    continue
                message = event.get('message') or {}
                usage = message.get('usage') if isinstance(message, dict) else None
                if isinstance(usage, dict):
                    occupied, window, basis = _input_sum(usage), None, 'transcript-estimate'
    if config.get('window_tokens') is not None:
        window = config['window_tokens']
    if window is not None and (not isinstance(window, (int, float)) or isinstance(window, bool) or window <= 0):
        raise ValueError('window_tokens must be positive')
    thresholds, mode = resolve_thresholds(args, config, window)
    level = 'unknown' if occupied is None else 'ok'
    if occupied is not None:
        for name, threshold in zip(('prepare', 'refresh', 'rotate'), thresholds):
            if occupied >= threshold:
                level = name
    return {'level': level, 'occupied_tokens': occupied, 'window_tokens': window,
            'percent': occupied * 100 / window if occupied is not None and window else None,
            'basis': basis, 'threshold_mode': mode}



SECTIONS = ('Resume prompt', 'Objective and acceptance', 'Authority and prohibitions',
            'State', 'Decisions', 'Running now', 'Pending user decisions',
            'Failed approaches', 'Verification', 'Next actions', 'Missing information')
RESUME_MARKER = '<!-- context-transfer:resume-check -->'
SECRET_PATTERNS = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in (
    ('ghp_', r'ghp_[A-Za-z0-9_]+'), ('github_pat_', r'github_pat_[A-Za-z0-9_]+'),
    ('glpat-', r'glpat-[A-Za-z0-9_-]+'), ('sk-', r'(?<![A-Za-z])sk-[A-Za-z0-9_-]+'),
    ('AKIA', r'AKIA[A-Z0-9]+'),
    ('xoxa-', r'xoxa-[A-Za-z0-9-]+'), ('xoxb-', r'xoxb-[A-Za-z0-9-]+'),
    ('xoxp-', r'xoxp-[A-Za-z0-9-]+'), ('xoxr-', r'xoxr-[A-Za-z0-9-]+'),
    ('xoxs-', r'xoxs-[A-Za-z0-9-]+'),
    ('private key', r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----'),
    ('password', r'\bpassword["\']?\s*[:=]\s*["\']?[^\s"\',}]+'),
    ('token', r'\btoken["\']?\s*[:=]\s*["\']?[^\s"\',}]+'),
)]


class ValidationError(Exception):
    pass


def parse_checkpoint(text):
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != '---':
        raise ValidationError('missing front matter opening delimiter')
    end = next ( (i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        raise ValidationError('missing front matter closing delimiter')
    try:
        meta = json.loads(''.join(lines[1:end]))
    except ValueError:
        raise ValidationError('invalid front matter JSON')
    if not isinstance(meta, dict):
        raise ValidationError('front matter must be an object')
    return meta, ''.join(lines[end + 1:])


def validate_checkpoint(meta, body, root, mode):
    errors, warnings = [], []
    def fields(obj, keys, label):
        if not isinstance(obj, dict):
            errors.append(label + ' must be an object')
            return
        for key in keys:
            if key not in obj:
                errors.append('missing field ' + label + '.' + key)
    model_keys = {'task', 'verification', 'continuation', 'artifacts', 'source'}
    if mode == 'draft':
        for key in meta.keys() - model_keys:
            errors.append('script-owned field ' + key)
        if 'source' in meta:
            if not isinstance(meta['source'], dict):
                errors.append('source must be an object')
            else:
                errors.extend('script-owned field source.' + key for key in meta['source'] if key != 'model')
    else:
        fields(meta, ('schema_version', 'transfer_id', 'created_at', 'previous_transfer_id',
                      'source', 'project', 'delegated_sessions'), 'checkpoint')
        version = meta.get('schema_version')
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            errors.append('invalid schema_version')
        elif version > 1:
            errors.append('unsupported schema_version; update the skill')
        source = meta.get('source')
        fields(source, ('agent', 'session_id', 'model', 'context'), 'source')
        if isinstance(source, dict):
            fields(source.get('context'), ('occupied_tokens', 'window_tokens', 'percent', 'basis', 'threshold_mode'), 'source.context')
        fields(meta.get('project'), ('root', 'worktree', 'git_common_dir', 'repository', 'branch',
                                     'head', 'dirty', 'untracked', 'dirty_digests'), 'project')
        project = meta.get('project')
        if isinstance(project, dict):
            for key in ('root', 'worktree'):
                if not isinstance(project.get(key), str):
                    errors.append('invalid project.' + key)
            for key in ('git_common_dir', 'repository', 'branch', 'head'):
                if project.get(key) is not None and not isinstance(project[key], str):
                    errors.append('invalid project.' + key)
            for key in ('dirty', 'untracked'):
                value = project.get(key)
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    errors.append('invalid project.' + key)
            digests = project.get('dirty_digests')
            if not isinstance(digests, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                        for k, v in digests.items()):
                errors.append('invalid project.dirty_digests')
        if isinstance(source, dict) and isinstance(source.get('context'), dict):
            context = source['context']
            for key in ('occupied_tokens', 'window_tokens', 'percent'):
                value = context.get(key)
                if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
                    errors.append('invalid source.context.' + key)
            for key in ('basis', 'threshold_mode'):
                value = context.get(key)
                if value is not None and not isinstance(value, str):
                    errors.append('invalid source.context.' + key)
        delegated = meta.get('delegated_sessions')
        if isinstance(delegated, list):
            for session in delegated:
                fields(session, ('session_id', 'task', 'family', 'model', 'effort', 'cli_session_id', 'state', 'alive'), 'delegated_sessions')
        elif not isinstance(delegated, dict) or not isinstance(delegated.get('not_captured'), str):
            errors.append('invalid delegated_sessions')
    fields(meta, ('task', 'verification', 'continuation', 'artifacts'), 'model')
    task, verification, continuation = meta.get('task'), meta.get('verification'), meta.get('continuation')
    fields(task, ('objective', 'status', 'next_action'), 'task')
    fields(verification, ('status', 'evidence'), 'verification')
    fields(continuation, ('recommended_skills', 'blockers', 'pending_decisions'), 'continuation')
    if isinstance(task, dict) and task.get('status') not in ('in-progress', 'blocked', 'awaiting-user', 'done'):
        errors.append('invalid task.status')
    if isinstance(verification, dict):
        if verification.get('status') not in ('verified', 'partial', 'unverified'):
            errors.append('invalid verification.status')
        evidence = verification.get('evidence')
        if not isinstance(evidence, list):
            errors.append('invalid verification.evidence')
        else:
            for item in evidence:
                fields(item, ('claim', 'check', 'result'), 'verification.evidence')
    if isinstance(continuation, dict):
        for key in ('recommended_skills', 'blockers', 'pending_decisions'):
            if not isinstance(continuation.get(key), list):
                errors.append('invalid continuation.' + key)
    artifacts = meta.get('artifacts')
    if not isinstance(artifacts, list):
        errors.append('invalid artifacts')
    else:
        for path in artifacts:
            if not isinstance(path, str) or Path(path).is_absolute():
                errors.append('invalid artifact path')
            elif isinstance(root, (str, Path)) and not (Path(root) / path).exists():
                display = '[redacted]' if _secret_errors(path) else path
                (errors if mode == 'draft' else warnings).append('missing artifact ' + display)
    positions = []
    for section in SECTIONS:
        found = re.search(r'^## ' + re.escape(section) + r'\s*$', body, re.MULTILINE)
        if not found:
            errors.append('missing section ' + section)
        else:
            positions.append(found.start())
    if positions != sorted(positions):
        errors.append('sections out of order')
    errors.extend(_secret_errors('---\n' + json.dumps(meta, indent=2) + '\n---\n' + body))
    return errors, warnings


def _secret_errors(text):
    errors = []
    for number, line in enumerate(text.splitlines(), 1):
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                errors.append('line ' + str(number) + ': secret pattern ' + name)
    return errors


def read_latest(store, worktree):
    pointer = Path(store) / ('latest-' + slug(worktree))
    if pointer.is_symlink():
        raise OSError('latest pointer is a symlink')
    try:
        value = pointer.read_text().strip()
    except FileNotFoundError:
        return None
    if not re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{8}', value):
        raise OSError('invalid latest pointer')
    path = Path(store) / (value + '.md')
    if path.is_symlink() or not path.is_file():
        raise OSError('invalid latest checkpoint')
    return value


def _without_resume(body):
    pattern = re.escape(RESUME_MARKER) + r'.*?' + re.escape(RESUME_MARKER) + r'\n?'
    return re.sub(pattern, '', body, flags=re.DOTALL)


def _resume_body(body, path, root):
    body = _without_resume(body)
    heading = re.search(r'^## Resume prompt\s*$', body, re.MULTILINE)
    next_heading = re.search(r'^## ', body[heading.end():], re.MULTILINE)
    end = heading.end() + next_heading.start() if next_heading else len(body)
    quoted = shlex.quote(str(root))
    block = (RESUME_MARKER + '\nCheckpoint: ' + str(path.resolve()) + '\n'
             + '```sh\n' + '\n'.join('git -C ' + quoted + ' ' + command for command in
                ('branch --show-current', 'rev-parse HEAD', 'status --porcelain'))
             + '\n```\n' + RESUME_MARKER + '\n')
    return body[:end] + block + body[end:]


def _atomic_write(path, data):
    fd, temp = tempfile.mkstemp(prefix='.context-transfer-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_checkpoint(draft_path, args):
    text = Path(draft_path).read_text(encoding='utf-8')
    meta, body = parse_checkpoint(text)
    facts = collect_facts(args.repo, getattr(args, 'host_context', None))
    project = facts['project']
    errors, warnings = validate_checkpoint(meta, body, project['root'], 'draft')
    errors = [error for error in errors if ': secret pattern ' not in error] + _secret_errors(text)
    if errors:
        raise ValidationError('; '.join(errors))
    if len(body) > args.budget:
        warnings.append('body exceeds budget')
    for warning in warnings:
        print('Warning: ' + warning, file=sys.stderr)
    store = resolve_store(args)
    previous = read_latest(store, project['worktree'])
    model = {key: meta.get(key) for key in ('task', 'verification', 'continuation', 'artifacts')}
    model['source'] = {'model': meta.get('source', {}).get('model')}
    if previous:
        old_path = store / (previous + '.md')
        old, old_body = parse_checkpoint(old_path.read_text(encoding='utf-8'))
        old_model = {key: old.get(key) for key in ('task', 'verification', 'continuation', 'artifacts')}
        old_model['source'] = {'model': old.get('source', {}).get('model')}
        keys = ('branch', 'head', 'dirty', 'untracked', 'dirty_digests')
        if model == old_model and facts['delegated_sessions'] == old.get('delegated_sessions') and _without_resume(body) == _without_resume(old_body) and all(project[k] == old.get('project', {}).get(k) for k in keys):
            return old_path
    now = datetime.datetime.now(datetime.timezone.utc)
    transfer_id = now.strftime('%Y%m%dT%H%M%SZ-') + os.urandom(4).hex()
    path = store / (transfer_id + '.md')
    env = os.environ
    session = env.get('CLAUDE_CODE_SESSION_ID') or env.get('CODEX_THREAD_ID')
    agent = 'claude-code' if env.get('CLAUDE_CODE_SESSION_ID') else 'codex' if env.get('CODEX_THREAD_ID') else 'other'
    usage = measure_usage(args, env, store)
    usage.pop('level', None)
    result = dict(model, schema_version=1, transfer_id=transfer_id,
                  created_at=now.isoformat().replace('+00:00', 'Z'), previous_transfer_id=previous,
                  project=project, delegated_sessions=facts['delegated_sessions'])
    result['source'].update(agent=agent, session_id=session, context=usage)
    final_body = _resume_body(body, path, project['root'])
    output = '---\n' + json.dumps(result, indent=2, ensure_ascii=True) + '\n---\n' + final_body
    errors, _ = validate_checkpoint(result, final_body, project['root'], 'checkpoint')
    errors += _secret_errors(output)
    if errors:
        raise ValidationError('; '.join(errors))
    installed = False
    try:
        _atomic_write(path, output.encode('utf-8'))
        installed = True
        reread = path.read_text(encoding='utf-8')
        reread_meta, reread_body = parse_checkpoint(reread)
        errors, _ = validate_checkpoint(reread_meta, reread_body, project['root'], 'checkpoint')
        if reread != output or errors or _secret_errors(reread):
            raise OSError('checkpoint readback failed')
        _atomic_write(store / ('latest-' + slug(project['worktree'])), (transfer_id + '\n').encode())
    except Exception as exc:
        if installed:
            try:
                path.unlink()
            except OSError as cleanup:
                raise OSError('write failed: ' + str(exc) + '; cleanup failed for ' + str(path) + ': ' + str(cleanup)) from exc
        raise
    return path


def _changed_since(repo, head):
    if not head:
        return set()
    raw = _git(repo, 'diff', '--name-status', '-z', head, '--')
    if raw is None:
        raise OSError('git diff against checkpoint HEAD failed')
    parts = raw.split(b'\0')
    result, index = set(), 0
    while index < len(parts) and parts[index]:
        status = parts[index]
        count = 2 if status.startswith((b'R', b'C')) else 1
        if index + count >= len(parts):
            raise OSError('invalid git diff output')
        result.update(os.fsdecode(name) for name in parts[index + 1:index + 1 + count])
        index += count + 1
    return result


def resume_check(target, args):
    current = _project(args.repo)
    store = resolve_store(args)
    result = {'verdict': None, 'transfer_id': None, 'path': None,
              'drift': [], 'missing': [], 'candidates': []}
    if target:
        name = str(target)
        path = store / (name + '.md') if re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{8}', name) else Path(name).expanduser().absolute()
    else:
        latest = read_latest(store, current['worktree'])
        if not latest:
            result['candidates'] = [str(p) for p in sorted(store.glob('*.md')) if p.is_file() and not p.is_symlink()]
            result['verdict'] = 'choose' if result['candidates'] else 'none'
            return result
        path = store / (latest + '.md')
    result['path'] = str(path)
    try:
        raw = path.read_text(encoding='utf-8')
        meta, body = parse_checkpoint(raw)
        root = meta.get('project', {}).get('root', current['root']) if isinstance(meta.get('project'), dict) else current['root']
        errors, _ = validate_checkpoint(meta, body, root, 'checkpoint')
        errors += _secret_errors(raw)
        if errors:
            result['verdict'] = 'invalid'
            return result
    except (ValidationError, UnicodeError, FileNotFoundError):
        result['verdict'] = 'invalid'
        return result
    result['transfer_id'] = meta['transfer_id']
    old = meta['project']
    old_common, new_common = old['git_common_dir'], current['git_common_dir']
    if old_common and new_common:
        same = Path(old_common).resolve() == Path(new_common).resolve()
        if not same and old['repository'] and current['repository']:
            same = normalize_remote(old['repository']) == normalize_remote(current['repository']) if '://' in old['repository'] or '@' in old['repository'] else old['repository'] == current['repository']
    elif old_common or new_common:
        same = False
    else:
        same = Path(old['root']).resolve() == Path(current['root']).resolve()
    if not same:
        result['verdict'] = 'blocked'
        return result
    drift = result['drift']
    for key in ('branch', 'head'):
        if old[key] != current[key]:
            drift.append({'kind': key, 'detail': {'saved': old[key], 'current': current[key]}})
    old_paths = set(old['dirty']) | set(old['untracked'])
    new_paths = set(current['dirty']) | set(current['untracked'])
    if old_paths != new_paths:
        drift.append({'kind': 'dirty-set', 'detail': {'saved': sorted(old_paths), 'current': sorted(new_paths)}})
    has_saved_head = old['head'] and _git(current['root'], 'cat-file', '-e', old['head'] + '^{commit}') is not None
    changed = _changed_since(current['root'], old['head']) if current['git_common_dir'] and has_saved_head else set(new_paths)
    changed.update(current['untracked'])
    digests = old['dirty_digests']
    content = {name for name in changed if name not in digests}
    for name, saved in digests.items():
        path_now = Path(current['root']) / name
        value = _path_digest(path_now)
        if value != saved:
            content.add(name)
    drift.extend({'kind': 'content', 'detail': name} for name in sorted(content))
    result['missing'] = [name for name in meta['artifacts'] if not (Path(current['root']) / name).exists()]
    result['verdict'] = 'drifted' if drift or result['missing'] else 'fresh'
    return result


def _linked_ids(store):
    linked = set()
    for pointer in Path(store).glob('latest-*'):
        if pointer.is_symlink() or not pointer.is_file():
            continue
        value = pointer.read_text(encoding='utf-8').strip()
        if re.fullmatch(r'\d{8}T\d{6}Z-[0-9a-f]{8}', value):
            linked.add(value)
    return linked


def list_checkpoints(store):
    store = Path(store)
    linked = _linked_ids(store)
    entries = []
    for path in sorted(store.glob('*.md')):
        if path.is_symlink() or not path.is_file():
            continue
        entry = {'id': path.stem, 'created_at': None, 'worktree': None,
                 'branch': None, 'head': None, 'validation': None,
                 'unlinked': path.stem not in linked}
        try:
            raw = path.read_text(encoding='utf-8')
            meta, body = parse_checkpoint(raw)
            project = meta.get('project')
            project = project if isinstance(project, dict) else {}
            entry.update(created_at=meta.get('created_at'), worktree=project.get('worktree'),
                         branch=project.get('branch'), head=project.get('head'))
            errors, _ = validate_checkpoint(meta, body, project.get('root') or os.getcwd(), 'checkpoint')
            errors = [error for error in errors if ': secret pattern ' not in error] + _secret_errors(raw)
            entry['validation'] = errors[0] if errors else 'ok'
        except (ValidationError, UnicodeError) as exc:
            entry['validation'] = str(exc)
        entries.append(entry)
    return entries


def prune(store, keep, all_worktrees, worktree):
    if keep < 1:
        raise ValueError('--keep must be at least 1')
    store = Path(store)
    groups = {}
    for entry in list_checkpoints(store):
        tree = entry['worktree']
        if isinstance(tree, str) and (all_worktrees or tree == worktree):
            groups.setdefault(tree, []).append(entry)
    removed = []
    for entries in groups.values():
        entries.sort(key=lambda entry: entry['id'], reverse=True)
        for entry in entries[keep:]:
            if entry['unlinked']:
                path = store / (entry['id'] + '.md')
                path.unlink()
                removed.append(path)
    return removed


def _common(parser):
    parser.add_argument('--repo', default=argparse.SUPPRESS)
    parser.add_argument('--store', default=argparse.SUPPRESS)
    parser.add_argument('--json', action='store_true', default=argparse.SUPPRESS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    _common(parser)
    subs = parser.add_subparsers(dest='command', required=True)
    for name in ('facts', 'usage', 'write', 'validate', 'resume-check', 'list', 'prune'):
        sub = subs.add_parser(name)
        _common(sub)
        if name in ('facts', 'write'):
            sub.add_argument('--host-context')
        if name in ('facts', 'usage', 'write'):
            sub.add_argument('--statusline', action='store_true')
            for option in ('transcript', 'rollout', 'prepare', 'refresh', 'rotate'):
                sub.add_argument('--' + option)
        if name in ('write', 'validate'):
            sub.add_argument('--budget', type=int, default=12000)
            sub.add_argument('path')
        if name == 'resume-check':
            sub.add_argument('path', nargs='?')
        if name == 'prune':
            sub.add_argument('--keep', type=int, required=True)
            sub.add_argument('--all', action='store_true')
    args = parser.parse_args(argv)
    try:
        args.repo = getattr(args, 'repo', None)
        if args.command in ('list', 'prune'):
            if args.command == 'prune' and args.keep < 1:
                parser.error('--keep must be at least 1')
            store = resolve_store(args)
            if args.command == 'list':
                result = {'checkpoints': list_checkpoints(store)}
            else:
                tree = _project(args.repo)['worktree']
                result = {'removed': [str(path) for path in prune(store, args.keep, args.all, tree)]}
            print(json.dumps(result, ensure_ascii=True, indent=None if getattr(args, 'json', False) else 2))
            return 0
        if args.command == 'resume-check':
            result = resume_check(args.path, args)
            print(json.dumps(result, ensure_ascii=True, indent=None if getattr(args, 'json', False) else 2))
            return 0 if result['verdict'] in ('fresh', 'drifted') else 1
        if args.command == 'write':
            path = write_checkpoint(args.path, args)
            _, body = parse_checkpoint(path.read_text(encoding='utf-8'))
            resume = body.split('## Resume prompt', 1)[1].split('## ', 1)[0].strip()
            print(json.dumps({'path': str(path), 'resume_prompt': resume}) if getattr(args, 'json', False) else str(path) + '\n' + resume)
            return 0
        if args.command == 'validate':
            raw = Path(args.path).read_text(encoding='utf-8')
            meta, body = parse_checkpoint(raw)
            root = meta.get('project', {}).get('root', args.repo or os.getcwd()) if isinstance(meta.get('project'), dict) else (args.repo or os.getcwd())
            errors, warnings = validate_checkpoint(meta, body, root, 'checkpoint')
            errors = [error for error in errors if ': secret pattern ' not in error] + _secret_errors(raw)
            if len(body) > args.budget:
                warnings.append('body exceeds budget')
            result = {'errors': errors, 'warnings': warnings}
            print(json.dumps(result) if getattr(args, 'json', False) else '\n'.join(errors + ['Warning: ' + w for w in warnings]) or 'valid')
            return 1 if errors else 0
        store = resolve_store(args)
        usage = measure_usage(args, os.environ, store)
        if args.command == 'usage':
            print(json.dumps(usage, ensure_ascii=True, indent=None if getattr(args, 'json', False) else 2))
            return 0
        facts = collect_facts(args.repo, getattr(args, 'host_context', None))
        facts['context'] = usage
        target = read_latest(store, facts['project']['worktree'])
        if target:
            facts['checkpoints'].append(str(store / (target + '.md')))
        print(json.dumps(facts, ensure_ascii=True, indent=None if getattr(args, 'json', False) else 2))
        return 0
    except ValidationError as exc:
        print(json.dumps({'errors': [str(exc)]}) if getattr(args, 'json', False) else str(exc), file=sys.stdout if getattr(args, 'json', False) else sys.stderr)
        return 1
    except ValueError as exc:
        parser.error(str(exc))
    except OSError as exc:
        print('I/O error: ' + str(exc), file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())

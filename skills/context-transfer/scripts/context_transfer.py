"""Collect local context and manage context-transfer checkpoints."""
import argparse
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
    return str(Path(path).resolve()).replace('/', '-')


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
        try:
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            digests[name] = digest.hexdigest()
        except FileNotFoundError:
            digests[name] = 'deleted'
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
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    store.chmod(0o700)
    return store


def collect_facts(repo):
    project = _project(repo)
    return {'project': project, 'checkpoints': [],
            'context': {'occupied_tokens': None, 'window_tokens': None, 'percent': None,
                        'basis': 'unknown', 'threshold_mode': None},
            'delegated_sessions': {'not_captured': 'no --host-context'}}


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
    if args.command != 'facts':
        parser.error(args.command + ' is not implemented yet')
    try:
        args.repo = getattr(args, 'repo', None)
        store = resolve_store(args)
        facts = collect_facts(args.repo)
        pointer = store / ('latest-' + slug(facts['project']['worktree']))
        if pointer.is_file() and not pointer.is_symlink():
            target = pointer.read_text().strip()
            if target and Path(target).name == target and (store / target).is_file():
                facts['checkpoints'].append(str(store / target))
        print(json.dumps(facts, ensure_ascii=True, indent=None if getattr(args, 'json', False) else 2))
        return 0
    except OSError as exc:
        print('I/O error: ' + str(exc), file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())

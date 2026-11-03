"""Git fixtures for context transfer tests."""
from pathlib import Path
import os
import subprocess


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
               GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
    return subprocess.run(['/usr/bin/git', '-C', str(repo), *args], env=env,
                          check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def make_repo(tmp, remote=None):
    repo = Path(tmp) / 'repo'
    repo.mkdir()
    git(repo, 'init')
    (repo / 'tracked').write_text('original')
    git(repo, 'add', 'tracked')
    git(repo, 'commit', '-m', 'initial')
    (repo / 'tracked').write_text('changed')
    (repo / 'untracked').write_text('new')
    if remote:
        git(repo, 'remote', 'add', 'origin', remote)
    return repo

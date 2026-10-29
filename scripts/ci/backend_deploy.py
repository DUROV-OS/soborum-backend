#!/usr/bin/python3 -I
"""Installed, reviewed root helper. Never executed from the application checkout."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

STATE = Path('/var/lib/soborum-backend-ci')
REPOSITORY = 'https://github.com/DUROV-OS/soborum-backend.git'
LEGACY = Path('/srv/soborbum-backend')
PROJECT = 'soborbum-backend'
CONTAINER = 'soborbum-backend-backend-1'
DATABASE = 'soborbum-backend-db-1'
STORAGE = Path('/var/lib/docker/volumes/soborbum-backend_storage_data/_data')
SAFE_ENV = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C.UTF-8',
            'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': '/dev/null',
            'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_COUNT': '0',
            'GIT_TERMINAL_PROMPT': '0', 'GIT_ATTR_NOSYSTEM': '1'}
CONFIGS = ('.env', '.env.mcp', 'docker-compose.override.yml')


class Rejected(RuntimeError):
    pass


def revision(value: str) -> str:
    if re.fullmatch('[0-9a-f]{40}', value) is None:
        raise Rejected('expected one lowercase 40-character commit SHA')
    return value


def command(value: str) -> str:
    # No shell parsing, quotes, options, trailing data or interactive sessions.
    if re.fullmatch('deploy [0-9a-f]{40}', value) is None:
        raise Rejected('only deploy <commit SHA> is allowed')
    return revision(value[7:])


def secure_path(path: Path) -> None:
    for item in (path, *path.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise Rejected('deployment control path is not protected')


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(args, *, data=None, stdin=None, stdout=subprocess.PIPE, timeout=300):
    try:
        result = subprocess.run(args, input=data, stdin=stdin, stdout=stdout,
                                stderr=subprocess.PIPE, env=SAFE_ENV,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Rejected('deployment subprocess failed or timed out') from error
    if result.returncode:
        # Subprocess output may contain private configuration. Do not echo it.
        raise Rejected('deployment subprocess returned nonzero status')
    return result.stdout


def git(*args, data=None):
    return run(['/usr/bin/git', '--git-dir=' + str(STATE / 'repository.git'),
                '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
                *args], data=data)


def fetch_main() -> str:
    git('fetch', '--no-tags', REPOSITORY, 'refs/heads/main')
    return revision(git('rev-parse', 'FETCH_HEAD').decode().strip())


def tree(sha: str) -> dict[str, tuple[int, bytes]]:
    entries = []
    for record in git('ls-tree', '-rz', revision(sha)).split(b'\0'):
        if not record:
            continue
        metadata, raw_path = record.split(b'\t', 1)
        mode, kind, oid = metadata.split()
        name = raw_path.decode('utf-8')
        parts = Path(name).parts
        if (mode not in (b'100644', b'100755') or kind != b'blob'
                or name.startswith('/') or any(p in ('..', '.git') for p in parts)):
            raise Rejected('unsupported repository entry')
        entries.append((name, int(mode, 8) & 0o777, oid))
    payload = git('cat-file', '--batch', data=b''.join(e[2] + b'\n' for e in entries))
    stream = io.BytesIO(payload)
    result = {}
    for name, mode, oid in entries:
        header = stream.readline().split()
        if len(header) != 3 or header[:2] != [oid, b'blob']:
            raise Rejected('invalid trusted object stream')
        size = int(header[2])
        contents = stream.read(size)
        if len(contents) != size or stream.read(1) != b'\n':
            raise Rejected('truncated trusted object stream')
        result[name] = (mode, contents)
    return result


def manifest(files, *, runtime=False):
    ignored = ('.github/', '.claude/', 'scripts/ci/', 'tests/', 'docs/')
    return {p: [mode, digest(data)] for p, (mode, data) in files.items()
            if not runtime or not p.startswith(ignored)}


def verify_files(root: Path, files) -> None:
    for name, (expected_mode, contents) in files.items():
        path = root / name
        for parent in path.parents:
            if parent == root.parent:
                break
            if parent.is_symlink():
                raise Rejected('dirty tree: symbolic-link directory')
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
                info = os.fstat(handle.fileno())
                actual = handle.read()
        except OSError as error:
            raise Rejected('dirty tree: missing or unreadable tracked file') from error
        if (not stat.S_ISREG(info.st_mode) or actual != contents
                or bool(info.st_mode & 0o100) != bool(expected_mode & 0o100)):
            raise Rejected('dirty tree: tracked source differs from trusted commit')


def verify_legacy(sha):
    verify_files(LEGACY, tree(sha))
    # Only the root-controlled Git directory is used. Never load legacy .git/config.
    git('read-tree', sha)
    extra = git('--work-tree=' + str(LEGACY), 'ls-files', '--others',
                '--exclude-standard', '--exclude=.git').decode().splitlines()
    if set(extra) - {'.env.mcp'}:
        raise Rejected('dirty tree: unexpected untracked files')


def config_hashes(root):
    result = {}
    for name in CONFIGS:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise Rejected('configuration path is missing or symbolic')
        result[name] = digest(path.read_bytes())
    return result


def verify_configs(expected):
    if config_hashes(LEGACY) != expected or config_hashes(STATE / 'config') != expected:
        raise Rejected('configuration changed; owner review is required')


@contextlib.contextmanager
def release_lock(path: Path):
    import fcntl
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Rejected('another backend release is active') from error
        yield
    finally:
        os.close(descriptor)


def write_state(value):
    temporary = STATE / 'state.json.new'
    temporary.write_text(json.dumps(value, sort_keys=True) + '\n')
    os.chmod(temporary, 0o600)
    os.replace(temporary, STATE / 'state.json')


def ensure_release(sha, files):
    destination = STATE / 'releases' / revision(sha)
    if destination.exists():
        secure_path(destination)
        verify_files(destination, files)
        return destination
    temporary = Path(tempfile.mkdtemp(prefix='stage-', dir=STATE / 'releases'))
    try:
        for name, (mode, contents) in files.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
            os.chmod(path, mode)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def compose(source, *args, image=None, quiet=False):
    override = STATE / 'compose-ci.json'
    if image:
        override = STATE / 'compose-rollback.json'
        override.write_text(json.dumps({'services': {'backend': {
            'image': image, 'env_file': [{'path': str(STATE / 'config' / p), 'required': True}
                                        for p in ('.env', '.env.mcp')]}}}))
    cmd = ['/usr/bin/docker', 'compose', '-p', PROJECT, '--project-directory', str(source)]
    for name in ('.env', '.env.mcp'):
        cmd += ['--env-file', str(STATE / 'config' / name)]
    cmd += ['-f', str(source / 'docker-compose.yml'), '-f', str(STATE / 'config' / 'docker-compose.override.yml'),
            '-f', str(override), *args]
    return run(cmd, stdout=subprocess.DEVNULL if quiet else subprocess.PIPE, timeout=900)


def container_image():
    value = run(['/usr/bin/docker', 'inspect', CONTAINER, '--format', '{{.Image}}']).decode().strip()
    if re.fullmatch(r'sha256:[0-9a-f]{64}', value) is None:
        raise Rejected('unexpected current image identifier')
    return value


def ready(timeout=90):
    end = time.monotonic() + timeout
    probe = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3)"
    while True:
        try:
            run(['/usr/bin/docker', 'exec', CONTAINER, 'python', '-c', probe], timeout=8)
            return
        except Rejected:
            if time.monotonic() >= end:
                raise Rejected('backend readiness failed')
            time.sleep(3)


def require_checks(sha):
    url = 'https://api.github.com/repos/DUROV-OS/soborum-backend/commits/' + revision(sha) + '/check-runs'
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                  'User-Agent': 'soborum-backend-ci'})
    with urllib.request.urlopen(request, timeout=20) as response:
        checks = json.load(response).get('check_runs', [])
    if not any(c.get('name') in ('checks', 'checks / checks') and c.get('head_sha') == sha
               and c.get('app', {}).get('slug') == 'github-actions'
               and c.get('conclusion') == 'success' and c.get('status') == 'completed' for c in checks):
        raise Rejected('successful GitHub Actions checks are required for this SHA')


def verify_tar(path):
    with tarfile.open(path, 'r:*') as archive:
        for member in archive:
            if member.isfile():
                with archive.extractfile(member) as data:
                    while data.read(1024 * 1024):
                        pass


def snapshot(sha):
    destination = Path(tempfile.mkdtemp(prefix=time.strftime('%Y%m%dT%H%M%SZ-'), dir=STATE / 'backups'))
    os.chmod(destination, 0o700)
    files = tree(sha)
    with tarfile.open(destination / 'code.tar.gz', 'w:gz') as archive:
        for name, (mode, data) in files.items():
            entry = tarfile.TarInfo(name); entry.size = len(data); entry.mode = mode
            archive.addfile(entry, io.BytesIO(data))
    with tarfile.open(destination / 'config.tar.gz', 'w:gz') as archive:
        for name in CONFIGS:
            archive.add(STATE / 'config' / name, arcname=name, recursive=False)
    database = destination / 'database.dump'
    with database.open('wb') as output:
        run(['/usr/bin/docker', 'exec', DATABASE, 'pg_dump', '-U', 'soborbum', '-d', 'soborbum', '-Fc'], stdout=output)
    with database.open('rb') as source:
        run(['/usr/bin/docker', 'exec', '-i', DATABASE, 'pg_restore', '--file=/dev/null'], stdin=source)
    run(['/usr/bin/tar', '--one-file-system', '--numeric-owner', '-czf', str(destination / 'storage.tar.gz'), '-C', str(STORAGE), '.'])
    previous_image = container_image()
    with (destination / 'image.tar').open('wb') as output:
        run(['/usr/bin/docker', 'image', 'save', previous_image], stdout=output, timeout=600)
    for name in ('code.tar.gz', 'config.tar.gz', 'storage.tar.gz', 'image.tar'):
        verify_tar(destination / name)
    inventory = {'revision': sha, 'image': previous_image, 'files': {}}
    for path in destination.iterdir():
        h = hashlib.sha256()
        with path.open('rb') as data:
            for block in iter(lambda: data.read(1024 * 1024), b''):
                h.update(block)
        inventory['files'][path.name] = {'bytes': path.stat().st_size, 'sha256': h.hexdigest()}
    (destination / 'manifest.json').write_text(json.dumps(inventory, sort_keys=True) + '\n')
    return destination, previous_image


def deploy(sha):
    sha = revision(sha)
    secure_path(STATE)
    with release_lock(STATE / 'deploy.lock'):
        state = json.loads((STATE / 'state.json').read_text())
        if fetch_main() != sha:
            raise Rejected('requested SHA is not the current main')
        git('merge-base', '--is-ancestor', state['verified_revision'], sha)
        require_checks(sha)
        verify_legacy(state['legacy_revision'])
        verify_configs(state['config_hashes'])
        active = tree(state['active_revision'])
        target = tree(sha)
        if state.get('active_source'):
            secure_path(Path(state['active_source']))
            verify_files(Path(state['active_source']), active)
        if manifest(active, runtime=True) == manifest(target, runtime=True):
            ready()
            state['verified_revision'] = sha
            write_state(state)
            return {'verified_revision': sha, 'active_application_revision': state['active_revision'],
                    'runtime_matches_requested_commit': True, 'restarted': False, 'ready': True}
        old_migrations = {p: v for p, v in manifest(active).items() if p.startswith('alembic/')}
        new_migrations = {p: v for p, v in manifest(target).items() if p.startswith('alembic/')}
        if old_migrations != new_migrations:
            raise Rejected('migration changes require separate owner review')
        source = ensure_release(sha, target)
        previous_source = ensure_release(state['active_revision'], active)
        compose(source, 'config', '--quiet')
        backup, previous_image = snapshot(state['active_revision'])
        compose(source, 'build', 'backend', quiet=True)
        try:
            compose(source, 'up', '-d', '--no-deps', '--no-build', 'backend', quiet=True)
            ready()
        except Rejected as original:
            try:
                compose(previous_source, 'up', '-d', '--no-deps', '--no-build', '--force-recreate', 'backend', image=previous_image, quiet=True)
                ready()
            except Rejected:
                raise Rejected('deployment failed; application rollback also needs owner attention') from original
            raise Rejected('deployment failed; previous application restored; database untouched') from original
        state.update(verified_revision=sha, active_revision=sha, active_source=str(source), last_backup=str(backup))
        write_state(state)
        return {'verified_revision': sha, 'active_application_revision': sha,
                'runtime_matches_requested_commit': True, 'restarted': True, 'ready': True,
                'backup': str(backup)}


def dispatcher():
    if len(sys.argv) != 1:
        raise Rejected('dispatcher takes no arguments')
    sha = command(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    result = subprocess.run(['/usr/bin/sudo', '-n', '/usr/local/sbin/soborum-backend-deploy'],
                            input=(sha + '\n').encode(), env=SAFE_ENV, check=False)
    return result.returncode


def main():
    try:
        os.umask(0o077)
        if Path(sys.argv[0]).name == 'durov-gh-backend':
            return dispatcher()
        if len(sys.argv) != 1 or os.geteuid() != 0:
            raise Rejected('root helper accepts no command-line arguments')
        secure_path(Path(__file__))
        raw = sys.stdin.buffer.read(82)
        if not re.fullmatch(b'[0-9a-f]{40}\n', raw):
            raise Rejected('invalid revision input')
        print(json.dumps(deploy(raw.decode().strip()), sort_keys=True))
        return 0
    except Exception as error:
        # Never include subprocess output, environment, file contents or credentials.
        message = str(error) if isinstance(error, Rejected) else type(error).__name__
        print('Rejected: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

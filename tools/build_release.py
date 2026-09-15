"""Build a new, allowlisted source candidate after local content/path checks."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat

TOP_FILES = {'README.md', 'LICENSE', 'NOTICE', 'pyproject.toml'}
TREES = {
    'src/suiteharness': {'.py', '.typed'},
    'tests': {'.py'},
    'requirements': {'.txt', '.lock', '.md'},
    'deploy/source': {'.py', '.yaml', '.md', '.dockerignore'},
    'tools': {'.py'},
    '.github/workflows': {'.yaml', '.yml'},
}
DOC_FILES = {'docs/source-delivery.zh-CN.md'}
FORBIDDEN_PARTS = {'__pycache__', 'data', 'state', 'logs', 'backups', 'secrets', '.git'}
PRIVATE_NETS = tuple(ipaddress.ip_network(value) for value in (
    '10' + '.0.0.0/8', '172' + '.16.0.0/12', '192' + '.168.0.0/16',
    '169' + '.254.0.0/16', 'fc00::/7', 'fe80::/10'))
TOKEN = re.compile(r'(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,})')
PRIVATE_KEY = re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----')
IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
IPV6 = re.compile(r'(?<![\w:])(?:[A-Fa-f0-9]{0,4}:){2,}[A-Fa-f0-9:]{0,4}(?![\w:])')
ASSIGNMENT = re.compile(r'''(?im)^\s*["']?(?:api_key|app_secret|client_secret|password|access_token)["']?\s*[:=]\s*["']([^"'\r\n]{16,})["']''')

# Reviewed security-test literals, not a blanket exemption for tests. New
# addresses still fail review; credentials/host paths are never exempted.
PUBLIC_NETWORK_LITERALS = {
    'tests/test_mcp_defaults.py': {'10.1.2.0', '10.1.2.3', '10.1.3.3'},
}
PUBLIC_NETWORK_LITERALS['tools/build_release.py'] = {
    'fc00::', 'fe80::', *PUBLIC_NETWORK_LITERALS['tests/test_mcp_defaults.py'],
}


class ReleaseError(ValueError):
    pass


def check_content(name: str, payload: bytes) -> None:
    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ReleaseError(f'non-UTF-8 content: {name}') from exc
    if TOKEN.search(text) or PRIVATE_KEY.search(text):
        raise ReleaseError(f'credential/private-key pattern: {name}')
    for match in ASSIGNMENT.finditer(text):
        value = match.group(1).lower()
        if not value.startswith(('replace-', 'example-', 'test-', '${')):
            raise ReleaseError(f'credential assignment: {name}')
    for pattern in (IPV4, IPV6):
        for match in pattern.finditer(text):
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            if any(address.version == net.version and address in net for net in PRIVATE_NETS):
                if str(address) not in PUBLIC_NETWORK_LITERALS.get(name, set()):
                    raise ReleaseError(f'private network address: {name}')
    # Corporate deployment paths/user homes never belong in a generic candidate.
    if re.search(r'(?i)(?:/(?:opt|home)/[A-Za-z0-9_.-][^\s"\']*|[A-Z]:[/\\]Users[/\\][^\s"\']+)', text):
        raise ReleaseError(f'host-specific path: {name}')


def source_files(source: Path) -> dict[str, bytes]:
    if source.is_symlink():
        raise ReleaseError('source root must not be a symlink')
    root = source.resolve(strict=True)
    result: dict[str, bytes] = {}

    def add(path: Path) -> None:
        relative = path.relative_to(root)
        if any(part in FORBIDDEN_PARTS or part.startswith('.pytest') for part in relative.parts):
            return
        if any(marker in path.name for marker in ('.before', '.bak', '.rollback', '~')):
            return
        if any(p.is_symlink() for p in (path, *path.parents) if p != root.parent):
            raise ReleaseError(f'symbolic link: {relative.as_posix()}')
        if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
            raise ReleaseError(f'not a regular file: {relative.as_posix()}')
        payload = path.read_bytes()
        check_content(relative.as_posix(), payload)
        result[relative.as_posix()] = payload

    for name in sorted(TOP_FILES):
        add(root / name)
    for name in sorted(DOC_FILES):
        if (root / name).exists():
            add(root / name)
    for tree, suffixes in TREES.items():
        directory = root / tree
        if directory.is_symlink():
            raise ReleaseError(f'symbolic link: {tree}')
        if not directory.exists():
            continue
        for folder, dirs, files in os.walk(directory, followlinks=False):
            for child in dirs:
                if (Path(folder) / child).is_symlink():
                    raise ReleaseError(f'symbolic link directory in {tree}')
            dirs[:] = [name for name in dirs if name not in FORBIDDEN_PARTS and not name.startswith('.')]
            for name in sorted(files):
                path = Path(folder) / name
                if path.suffix in suffixes or name in {'Dockerfile', '.dockerignore', 'py.typed'}:
                    add(path)
    if 'src/suiteharness/__init__.py' not in result:
        raise ReleaseError('framework source entry is missing')
    return result


def build_release(source: Path, output: Path) -> int:
    if output.exists() or output.is_symlink():
        raise ReleaseError('output already exists; choose a new candidate directory')
    if source.resolve() == output.resolve() or source.resolve().is_relative_to(output.resolve()):
        raise ReleaseError('output cannot be the source or its parent')
    files = source_files(source)
    # All checks finish before any destination is created. Exclusive file opens
    # avoid overwrites; an I/O failure leaves an incomplete, unannounced candidate.
    output.mkdir(parents=False, exist_ok=False)
    for name, payload in sorted(files.items()):
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as handle:
            handle.write(payload)
    manifest = {'schema': 1, 'kind': 'source-candidate', 'files': {
        name: hashlib.sha256(payload).hexdigest() for name, payload in sorted(files.items())}}
    with (output / 'RELEASE-MANIFEST.json').open('x', encoding='utf-8') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write('\n')
    return len(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        count = build_release(args.source, args.output)
    except (ReleaseError, OSError) as exc:
        # Error names/paths are useful; never echo file contents.
        print(str(exc) if isinstance(exc, ReleaseError) else 'filesystem operation failed')
        return 1
    print(f'Created source candidate with {count} files; no publication performed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Delivery checks run real CLIs and verify filesystem/security boundaries."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def command(*args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'), PYTHONDONTWRITEBYTECODE='1')
    return subprocess.run([sys.executable, '-B', *map(str, args)], env=env,
                          cwd=ROOT, capture_output=True, text=True, timeout=30)


def fixture_source(tmp_path):
    root = tmp_path / 'source'
    (root / 'src/suiteharness').mkdir(parents=True)
    (root / 'src/suiteharness/__init__.py').write_text('"""Fixture."""\n')
    (root / 'LICENSE').write_text('MIT License\n')
    (root / 'NOTICE').write_text('Fixture provenance\n')
    (root / 'README.md').write_text('Public source\n')
    (root / 'pyproject.toml').write_text('[project]\nname="fixture"\n')
    return root


def release(source, output):
    return command(ROOT / 'tools/build_release.py', '--source', source, '--output', output)


def test_doctor_missing_config_fails_without_writing_or_echoing_secrets(tmp_path):
    private = tmp_path / 'private.yaml'
    marker = 'sensitive-' + 'value-that-must-not-appear'
    private.write_text('token: ' + marker)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    result = command('-m', 'suiteharness.doctor', '--config', tmp_path / 'absent.yaml',
                     '--secrets', private, '--json')
    assert result.returncode == 1, result.stderr
    assert result.stdout.strip(), result.stderr
    payload = json.loads(result.stdout)
    assert not payload['ok']
    assert marker not in result.stdout + result.stderr
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_doctor_source_inspection_is_explicitly_not_runtime_readiness():
    result = command('-m', 'suiteharness.doctor', '--source', ROOT, '--json')
    assert result.stdout.strip(), result.stderr
    payload = json.loads(result.stdout)
    assert payload['runtime_readiness'] == 'not_checked'
    assert any(c['name'] == 'source' and c['status'] == 'pass' for c in payload['checks'])


def test_release_is_allowlisted_and_hash_manifest_matches(tmp_path):
    source = fixture_source(tmp_path)
    (source / '.env').write_text('PRIVATE=yes')
    (source / 'business').mkdir()
    (source / 'business/customer.json').write_text('{"private":true}')
    (source / 'src/suiteharness/cache.py.bak').write_text('backup')
    output = tmp_path / 'candidate'
    result = release(source, output)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (output / '.env').exists()
    assert not (output / 'business').exists()
    assert not (output / 'src/suiteharness/cache.py.bak').exists()
    manifest = json.loads((output / 'RELEASE-MANIFEST.json').read_text())
    assert manifest['files']['src/suiteharness/__init__.py'] == hashlib.sha256(
        (output / 'src/suiteharness/__init__.py').read_bytes()).hexdigest()


@pytest.mark.parametrize('payload', [
    'address = "' + '.'.join(map(str, [192, 168, 23, 45])) + '"',
    'API_KEY = "' + 'sk-' + 'a' * 32 + '"',
    'key = "-----BEGIN ' + 'PRIVATE KEY-----"',
])
def test_release_rejects_sensitive_allowed_source_before_creating_output(tmp_path, payload):
    source = fixture_source(tmp_path)
    (source / 'src/suiteharness/leak.py').write_text(payload)
    output = tmp_path / 'candidate'
    result = release(source, output)
    assert result.returncode == 1
    assert not output.exists()
    assert payload not in result.stdout + result.stderr


def test_release_never_overwrites_an_existing_directory(tmp_path):
    source = fixture_source(tmp_path)
    output = tmp_path / 'existing'
    output.mkdir()
    marker = output / 'important.txt'
    marker.write_text('keep')
    result = release(source, output)
    assert result.returncode == 1
    assert marker.read_text() == 'keep'


def test_release_rejects_symlinks_inside_allowed_tree(tmp_path):
    source = fixture_source(tmp_path)
    private = tmp_path / 'private.py'
    private.write_text('private')
    try:
        (source / 'src/suiteharness/link.py').symlink_to(private)
    except OSError:
        pytest.skip('symlink creation requires platform permission')
    result = release(source, tmp_path / 'candidate')
    assert result.returncode == 1
    assert not (tmp_path / 'candidate').exists()


def test_real_checkout_builds_clean_candidate(tmp_path):
    result = release(ROOT, tmp_path / 'real-candidate')
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / 'real-candidate/NOTICE').is_file()
    assert (tmp_path / 'real-candidate/deploy/source/run.py').is_file()


def test_test_fixture_exception_does_not_allow_other_private_addresses(tmp_path):
    import runpy
    checker = runpy.run_path(str(ROOT / 'tools/build_release.py'))
    payload = ('target = "' + '.'.join(map(str, [192, 168, 99, 99])) + '"').encode()
    with pytest.raises(ValueError, match='private network'):
        checker['check_content']('tests/test_mcp_defaults.py', payload)

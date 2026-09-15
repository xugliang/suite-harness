"""Read-only source/configuration diagnostics; never activates a runtime."""
from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tomllib


def diagnose(source: Path, config: Path | None = None, secrets: Path | None = None) -> dict:
    checks: list[dict[str, str]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({'name': name, 'status': 'pass' if ok else 'fail', 'detail': detail})

    record('python', sys.version_info >= (3, 11), 'Python 3.11 or newer required')
    record('source', (source / 'src/suiteharness/__init__.py').is_file()
           and (source / 'pyproject.toml').is_file(), 'framework source and project metadata')
    try:
        from packaging.requirements import Requirement
        project = tomllib.loads((source / 'pyproject.toml').read_text(encoding='utf-8'))
        for entry in project.get('project', {}).get('dependencies', []):
            requirement = Requirement(entry)
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            try:
                version = metadata.version(requirement.name)
                valid = requirement.specifier.contains(version)
            except metadata.PackageNotFoundError:
                valid = False
            record('dependency:' + requirement.name, valid, 'installed distribution satisfies project requirement')
    except (ImportError, OSError, ValueError, KeyError):
        record('dependencies', False, 'cannot read/validate dependency metadata')

    if (config is None) != (secrets is None):
        record('config', False, 'both --config and --secrets are required together')
    elif config is not None and secrets is not None:
        try:
            from suiteharness.config import SuiteHarnessConfigLoader
            loaded = SuiteHarnessConfigLoader().load(config, secrets)
        except Exception:
            # Never expose exception input, YAML snippets or credential values.
            record('config', False, 'configuration or secrets could not be validated; values redacted')
        else:
            record('config', True, 'schema, references and production placeholder checks passed')
            for name, path in [('workspace', loaded.config.workspace.root),
                               ('storage', loaded.config.storage.root)]:
                record('path:' + name, path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK),
                       'directory must already exist and be accessible to this user; no files were created')
            sandbox = loaded.config.sandbox
            if sandbox.backend == 'docker':
                broker = sandbox.broker_socket
                if broker is None:
                    record('sandbox:binary', shutil.which(sandbox.binary) is not None,
                           'Docker executable lookup only; daemon/rootless/cgroup enforcement not checked')
                else:
                    try:
                        mode = broker.stat().st_mode
                        valid = stat.S_ISSOCK(mode)
                    except OSError:
                        valid = False
                    record('sandbox:broker', valid,
                           'broker socket presence only; no connection or process was started')
    else:
        checks.append({'name': 'config', 'status': 'not_checked',
                       'detail': 'pass explicit config and secrets paths to validate deployment settings'})
    return {'ok': all(item['status'] != 'fail' for item in checks),
            'runtime_readiness': 'not_checked', 'checks': checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--config', type=Path)
    parser.add_argument('--secrets', type=Path)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    result = diagnose(args.source, args.config, args.secrets)
    if args.json:
        print(json.dumps(result, ensure_ascii=True))
    else:
        for check in result['checks']:
            print(f"{check['status']}: {check['name']} — {check['detail']}")
        print('Runtime readiness was not checked; no service or container was started.')
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

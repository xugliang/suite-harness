"""Source deployment: python -m suiteharness.organization --config ... --secrets ..."""
import argparse
import json
from pathlib import Path

import yaml

from .config import DirectoryConfig, load_directory_config


def main():
    parser = argparse.ArgumentParser(description='Optional company organization directory')
    parser.add_argument('--config', required=True)
    parser.add_argument('--secrets', required=True)
    parser.add_argument('command', choices=['serve', 'migrate', 'status', 'bind', 'activate-config'])
    parser.add_argument('--expected-config-fingerprint')
    parser.add_argument('--product')
    parser.add_argument('--principal')
    parser.add_argument('--identity-kind', choices=['user_id', 'union_id', 'open_id'])
    parser.add_argument('--identity-value')
    parser.add_argument('--app-id', default='')
    args = parser.parse_args()
    config = DirectoryConfig.model_validate(yaml.safe_load(Path(args.config).read_text(encoding='utf-8')) or {})
    secrets = yaml.safe_load(Path(args.secrets).read_text(encoding='utf-8')) or {}
    credentials = load_directory_config(config, secrets)
    if credentials is None:
        print(json.dumps({'status': 'disabled'}))
        return
    if args.command == 'serve':
        import uvicorn

        from .http import create_directory_app
        uvicorn.run(create_directory_app(config, secrets), host=config.listen_host, port=config.listen_port, access_log=False)
        return
    from .postgres import PostgresDirectoryStore
    store = PostgresDirectoryStore(credentials['dsn'].get_secret_value(), expected_database=config.database_name)
    try:
        if args.command == 'migrate':
            store.migrate()
            print(json.dumps({'status': 'migrated'}))
        elif args.command == 'status':
            print(json.dumps(store.status(), ensure_ascii=False))
        elif args.command == 'activate-config':
            if not args.expected_config_fingerprint:
                parser.error('activate-config requires the previously inspected configuration fingerprint')
            store.activate_config(config.fingerprint, expected_fingerprint=args.expected_config_fingerprint)
            print(json.dumps({'status': 'configuration_activated'}))
        else:
            if not all([args.product, args.principal, args.identity_kind, args.identity_value]):
                parser.error('bind requires verified product, principal and external identity')
            store.bind(args.product, args.principal, kind=args.identity_kind, value=args.identity_value, app_id=args.app_id)
            print(json.dumps({'status': 'bound'}))
    finally:
        store.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Config parsing and database exceptions can contain secret-bearing text.
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None

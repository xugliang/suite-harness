"""Authenticated internal query surface; no organizational or business writes."""
from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager
from functools import partial

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import load_directory_config
from .service import DirectoryCoordinator


def create_directory_app(config, secrets, *, store=None, source=None):
    credentials = load_directory_config(config, secrets)
    if credentials is None:
        return Starlette(routes=[Route('/healthz', lambda request: JSONResponse({'status': 'disabled'}))])
    if store is None:
        from .postgres import PostgresDirectoryStore
        store = PostgresDirectoryStore(credentials['dsn'].get_secret_value(), expected_database=config.database_name)
    transport = None
    if source is None:
        from suiteharness.channels.feishu.client import HttpxFeishuTransport

        from .feishu import FeishuOrganizationSource
        transport = HttpxFeishuTransport()
        source = FeishuOrganizationSource(transport, app_id=config.app_id, app_secret=credentials['app_secret'])
    coordinator = DirectoryCoordinator(store, source, config)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(store.open)
        coordinator.start()
        try:
            yield
        finally:
            await coordinator.close()
            if transport is not None:
                await transport.close()
            await asyncio.to_thread(store.close)

    def authenticate(request):
        header = request.headers.get('authorization', '')
        if not header.startswith('Bearer ') or len(header) > 1024:
            return None
        token = header[7:]
        for product, expected in credentials['tokens'].items():
            if hmac.compare_digest(token.encode(), expected.get_secret_value().encode()):
                return product
        return None

    async def status(request):
        if authenticate(request) is None:
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        return JSONResponse(await coordinator.status())

    async def health(request):
        return JSONResponse({'status': 'ok'})

    async def acquire(request):
        product = authenticate(request)
        if product is None:
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        return JSONResponse(await coordinator.acquire(product))

    async def release(request):
        product = authenticate(request)
        if product is None:
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        await asyncio.to_thread(store.release, product)
        return JSONResponse({'status': 'released'})

    def project(record, product):
        return None if record is None else {key: value for key, value in record.items() if key in config.clients[product].fields}

    async def query(request):
        product = authenticate(request)
        if product is None:
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        try:
            state = await asyncio.to_thread(store.status)
            if state.get('last_success_at') and state.get('config_fingerprint') != config.fingerprint:
                return JSONResponse({'error': 'configuration_refresh_pending'}, status_code=503)
            if request.url.path.endswith('/profile'):
                principal = request.query_params.get('principal_id', '')
                if not principal or len(principal) > 256 or '\x00' in principal:
                    raise ValueError()
                record = await asyncio.to_thread(store.get_for_principal, product, principal)
                return JSONResponse({'employee': project(record, product)})
            if not config.clients[product].allow_directory_search:
                return JSONResponse({'error': 'forbidden'}, status_code=403)
            if request.url.path.endswith('/departments'):
                departments = await asyncio.to_thread(store.list_departments)
                allowed = config.clients[product].department_fields
                return JSONResponse({'departments': [
                    {key: value for key, value in {'department_id': row['source_id'], 'name': row['name'], 'parent_id': row['parent_id']}.items() if key in allowed}
                    for row in departments
                ]})
            limit = int(request.query_params.get('limit', '100'))
            offset = int(request.query_params.get('offset', '0'))
            records = await asyncio.to_thread(partial(store.list_employees, query=request.query_params.get('name'), limit=limit, offset=offset))
            return JSONResponse({'employees': [project(row, product) for row in records], 'next_offset': offset+limit if len(records) == limit else None})
        except ValueError:
            return JSONResponse({'error': 'invalid_directory_query'}, status_code=400)

    app = Starlette(lifespan=lifespan, routes=[
        Route('/healthz', health), Route('/v1/organization/status', status),
        Route('/v1/organization/acquire', acquire, methods=['POST']),
        Route('/v1/organization/release', release, methods=['POST']),
        Route('/v1/organization/profile', query), Route('/v1/organization/employees', query),
        Route('/v1/organization/departments', query),
    ])
    app.state.directory_coordinator = coordinator
    return app

"""Optional product client; no Feishu credentials or direct database access."""
from __future__ import annotations

import asyncio
import ipaddress
from contextlib import suppress
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr


class OrganizationDirectoryClient:
    def __init__(self, base_url, token, *, product_id, transport=None, heartbeat_seconds=120):
        url = urlsplit(base_url)
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in {'', '/'}:
            raise ValueError('invalid organization directory URL')
        if url.scheme == 'http' and url.hostname != 'localhost':
            try:
                private = ipaddress.ip_address(url.hostname).is_private
            except ValueError:
                private = False
            if not private:
                raise ValueError('directory HTTP requires an internal IP address')
        secret = token.get_secret_value() if isinstance(token, SecretStr) else token
        if not isinstance(secret, str) or len(secret) < 32 or not isinstance(product_id, str) or not product_id:
            raise ValueError('directory client credentials invalid')
        if type(heartbeat_seconds) is not int or not 10 <= heartbeat_seconds <= 300:
            raise ValueError('invalid directory heartbeat')
        self.product_id = product_id
        self._heartbeat_seconds = heartbeat_seconds
        self._http = httpx.AsyncClient(base_url=base_url.rstrip('/'), headers={'Authorization': 'Bearer '+secret}, timeout=10, follow_redirects=False, trust_env=False, transport=transport)
        self._task = None

    def __repr__(self):
        return 'OrganizationDirectoryClient(<redacted>)'

    async def _request(self, method, path, **kwargs):
        try:
            response = await self._http.request(method, path, **kwargs)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (httpx.HTTPError, ValueError):
            raise RuntimeError('organization directory unavailable') from None

    async def acquire(self):
        # Start heartbeat even when the first request fails: a directory restart
        # must not require restarting every product to re-register its demand.
        if self._task is None:
            self._task = asyncio.create_task(self._heartbeat())
        return await self._request('POST', '/v1/organization/acquire')

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            with suppress(RuntimeError):
                await self._request('POST', '/v1/organization/acquire')

    async def get_employee_for_principal(self, principal_id):
        if not isinstance(principal_id, str) or not principal_id or len(principal_id) > 256:
            raise ValueError('invalid principal')
        return (await self._request('GET', '/v1/organization/profile', params={'principal_id': principal_id})).get('employee')

    async def list_departments(self):
        return (await self._request('GET', '/v1/organization/departments')).get('departments', [])

    async def search_employees(self, name, *, limit=100):
        if not isinstance(name, str) or not name or len(name) > 256 or type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('invalid employee query')
        return (await self._request('GET', '/v1/organization/employees', params={'name': name, 'limit': limit})).get('employees', [])

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            with suppress(RuntimeError):
                await self._request('POST', '/v1/organization/release')
        await self._http.aclose()

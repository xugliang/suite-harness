"""Product opt-in, shared synchronization, bounded retries and lease fencing."""
from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from functools import partial

from suiteharness.diagnostics import emit

from .source import SourceError


class DirectoryCoordinator:
    def __init__(self, store, source, config):
        self.store, self.source, self.config = store, source, config
        self.owner = str(uuid.uuid4())
        self._wake = asyncio.Event()
        self._task = None

    async def _call(self, method, *args, **kwargs):
        return await asyncio.to_thread(partial(getattr(self.store, method), *args, **kwargs))

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def acquire(self, product_id):
        await self._call('acquire', product_id, ttl_seconds=self.config.consumer_ttl_seconds)
        self._wake.set()
        return await self.status()

    async def status(self):
        state = await self._call('status')
        from datetime import UTC, datetime
        last = state.get('last_success_at')
        if state.get('restricted'):
            status = 'restricted'
        elif last and state.get('config_fingerprint') != self.config.fingerprint:
            status = 'pending'
        elif not last:
            status = 'pending'
        elif (datetime.now(UTC)-datetime.fromisoformat(last)).total_seconds() >= self.config.refresh_seconds:
            status = 'stale'
        else:
            status = 'ready'
        return {'status': status, **{k: v for k, v in state.items() if k not in {'config_fingerprint', 'active_config_fingerprint'}}}

    async def _run(self):
        while True:
            self._wake.clear()
            try:
                if await self._call('has_consumers'):
                    await self.synchronize()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                emit('organization.sync', status='failed', reason='directory_storage_unavailable', exception_type=type(exc).__name__)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.config.check_interval_seconds)
            except TimeoutError:
                pass

    async def synchronize(self):
        claim = await self._call('claim', self.config.fingerprint, owner=self.owner, fresh_seconds=self.config.refresh_seconds)
        if claim['status'] != 'claimed':
            return claim
        token = claim['token']
        heartbeat = None
        fetch = None
        lease_lost = False
        code = 'source_unavailable'
        try:
            async def keep_lease():
                nonlocal lease_lost
                while True:
                    await asyncio.sleep(30)
                    try:
                        retained = await self._call('renew', owner=self.owner, token=token)
                    except Exception:
                        retained = False
                    if not retained:
                        lease_lost = True
                        if fetch is not None:
                            fetch.cancel()
                        return
            heartbeat = asyncio.create_task(keep_lease())
            for attempt in range(3):
                try:
                    fetch = asyncio.create_task(self.source.fetch())
                    snapshot = await asyncio.wait_for(fetch, timeout=120)
                    counts = await self._call('publish', snapshot, owner=self.owner, token=token, config_fingerprint=self.config.fingerprint)
                    emit('organization.sync', status='succeeded', result_count=counts['total'])
                    return {'status': 'succeeded', 'counts': counts}
                except SourceError as exc:
                    code = exc.code
                    if not exc.retryable or attempt == 2:
                        break
                except TimeoutError:
                    code = 'source_timeout'
                    if attempt == 2:
                        break
                except ValueError as exc:
                    code = 'scope_changed' if 'scope changed' in str(exc) else ('identity_conflict' if 'identity' in str(exc) else 'snapshot_rejected')
                    break
                except Exception:
                    code = 'source_unavailable'
                    break
                if attempt < 2:
                    await asyncio.sleep(1 + 2*attempt)
            restricted = code in {'scope_changed', 'identity_conflict', 'permission_denied', 'insufficient_permissions', 'authentication_failed', 'missing_employee_fields', 'missing_department_fields', 'unauthorized'}
            await self._call('fail', owner=self.owner, token=token, code=code, restricted=restricted, retry_seconds=300)
            emit('organization.sync', status='failed', reason=code)
            return {'status': 'failed', 'error_code': code}
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if lease_lost and current is not None and not current.cancelling():
                return {'status': 'lease_lost'}
            with suppress(Exception):
                await asyncio.shield(self._call('fail', owner=self.owner, token=token, code='sync_cancelled', retry_seconds=30))
            raise
        finally:
            if fetch is not None and not fetch.done():
                fetch.cancel()
                with suppress(asyncio.CancelledError):
                    await fetch
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

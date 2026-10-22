import asyncio

import httpx
import pytest
from pydantic import SecretStr

from suiteharness.organization.client import OrganizationDirectoryClient
from suiteharness.organization.config import DirectoryConfig, load_directory_config
from suiteharness.organization.http import create_directory_app


def test_disabled_configuration_has_no_database_or_app_requirements():
    cfg = DirectoryConfig.model_validate({})
    assert cfg.enabled is False
    assert load_directory_config(cfg, {}) is None


def test_enabled_configuration_rejects_missing_secrets_without_echoing_values():
    cfg = DirectoryConfig.model_validate({'enabled': True, 'app_id': 'cli_test', 'clients': {'a': {'credentials_ref': 'a'}}})
    with pytest.raises(ValueError, match='credentials'):
        load_directory_config(cfg, {})


class Store:
    def __init__(self):
        self.acquired = []
    def open(self):
        pass
    def close(self):
        pass
    def acquire(self, product_id, **kwargs):
        self.acquired.append(product_id)
    def release(self, product_id):
        pass
    def status(self):
        return {'last_success_at': None, 'restricted': False, 'revision': 0}
    def get_for_principal(self, product_id, principal_id):
        return {'employee_id': 'id1', 'display_name': '员工甲', 'job_title': '工程师', 'status': 'active', 'department_ids': ['d1'], 'department_names': ['研发部'], 'revision': 1, 'updated_at': '2026-10-20T10:00:00+00:00', 'source_id': 'u1'} if product_id == 'a' and principal_id == 'p1' else None
    def list_employees(self, **kwargs):
        return [self.get_for_principal('a', 'p1')]
    def list_departments(self):
        return [{'source_id': 'd1', 'name': '研发部', 'parent_id': '0', 'leader_id': 'u1'}]


def test_authentication_binds_product_and_does_not_expose_source_identifiers():
    async def run():
        cfg = DirectoryConfig.model_validate({'enabled': True, 'app_id': 'cli_test', 'clients': {'a': {'credentials_ref': 'a', 'department_fields': ['name']}, 'b': {'credentials_ref': 'b', 'allow_directory_search': False}}})
        secrets = {'databases': {'organization_database': {'dsn': 'postgresql://unused/organization_directory'}}, 'feishu': {'organization_feishu': {'app_secret': 'unused-secret'}}, 'services': {'a': {'token': 'a'*40}, 'b': {'token': 'b'*40}}}
        store = Store()
        app = create_directory_app(cfg, secrets, store=store, source=object())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.post('/v1/organization/acquire')).status_code == 401
            assert store.acquired == []
            r = await client.post('/v1/organization/acquire', headers={'Authorization': 'Bearer '+'a'*40}, json={'product_id': 'b'})
            assert r.status_code == 200
            assert store.acquired == ['a']
            r = await client.get('/v1/organization/profile', params={'principal_id': 'p1'}, headers={'Authorization': 'Bearer '+'a'*40})
            assert r.json()['employee']['display_name'] == '员工甲'
            assert 'source_id' not in r.json()['employee']
            departments = await client.get('/v1/organization/departments', headers={'Authorization': 'Bearer '+'a'*40})
            assert departments.json() == {'departments': [{'name': '研发部'}]}
            r = await client.get('/v1/organization/profile', params={'principal_id': 'p1'}, headers={'Authorization': 'Bearer '+'b'*40})
            assert r.json()['employee'] is None
            assert (await client.get('/v1/organization/employees', headers={'Authorization': 'Bearer '+'b'*40})).status_code == 403
    asyncio.run(run())


def test_lease_loss_cancels_only_current_fetch_and_does_not_shutdown_coordinator(monkeypatch):
    from suiteharness.organization.service import DirectoryCoordinator
    original_sleep = asyncio.sleep
    async def fast_heartbeat(delay):
        await original_sleep(0 if delay == 30 else delay)
    monkeypatch.setattr('suiteharness.organization.service.asyncio.sleep', fast_heartbeat)
    async def run():
        class LeaseStore:
            def claim(self, *args, **kwargs):
                return {'status': 'claimed', 'token': 1}
            def renew(self, **kwargs):
                return False
            def fail(self, **kwargs):
                raise AssertionError('lost owner must not write a shutdown failure')
        class Source:
            async def fetch(self):
                await asyncio.Event().wait()
        coordinator = DirectoryCoordinator(LeaseStore(), Source(), DirectoryConfig(enabled=True, app_id='cli_test'))
        result = await asyncio.wait_for(coordinator.synchronize(), timeout=2)
        assert result['status'] == 'lease_lost'
        assert not asyncio.current_task().cancelling()
    asyncio.run(run())


def test_client_acquires_and_reads_only_its_bound_profile():
    async def run():
        seen = []
        def handler(request):
            seen.append(request)
            if request.url.path.endswith('/acquire'):
                return httpx.Response(200, json={'status': 'pending'})
            return httpx.Response(200, json={'employee': {'display_name': '员工甲'}})
        client = OrganizationDirectoryClient('http://127.0.0.1:18797', SecretStr('x'*40), product_id='a', transport=httpx.MockTransport(handler), heartbeat_seconds=300)
        try:
            assert (await client.acquire())['status'] == 'pending'
            assert (await client.get_employee_for_principal('p1'))['display_name'] == '员工甲'
            assert seen[1].url.params['principal_id'] == 'p1'
            assert 'product_id' not in seen[1].url.params
            assert 'x'*40 not in repr(client)
        finally:
            await client.close()
    asyncio.run(run())

"""Offline directory contracts at the real provider transport boundary."""

import asyncio
import dataclasses
import importlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from suiteharness.channels.feishu.client import FeishuHttpTransport
from suiteharness.channels.feishu.models import FeishuHttpResponse


def modules():
    return (
        importlib.import_module("suiteharness.organization.models"),
        importlib.import_module("suiteharness.organization.source"),
        importlib.import_module("suiteharness.organization.feishu"),
    )


def response(data=None, *, code=0, status=200, message=""):
    return FeishuHttpResponse(status, json.dumps({"code": code, "msg": message, "data": data}).encode())


def page(items=(), **overrides):
    return {"items": list(items), "has_more": False, **overrides}


def user(user_id="u1", **overrides):
    return {
        "user_id": user_id, "name": "张三", "department_ids": ["d1"],
        "job_title": "工程师", "open_id": "ou_" + user_id, "union_id": "on_" + user_id,
        "status": {"is_activated": True, "is_resigned": False}, **overrides,
    }


def department(department_id="d1", **overrides):
    return {"department_id": department_id, "name": "研发部", "parent_department_id": "0", **overrides}


class Token:
    async def get_token(self):
        return "fixture-token"


class Routes(FeishuHttpTransport):
    """Allow only explicitly modeled read requests, never the real network."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    async def send(self, request):
        self.requests.append(request)
        parsed = urlsplit(request.url)
        key = parsed.path.removeprefix("/open-apis/contact/v3/")
        assert request.method == "GET"
        assert request.json_body is None
        assert request.headers["Authorization"] == "Bearer fixture-token"
        query = parse_qs(parsed.query)
        assert query["user_id_type"] == ["user_id"]
        assert query["department_id_type"] == ["department_id"]
        route = self.routes[key]
        if callable(route):
            return await route(query)
        if isinstance(route, list):
            return route.pop(0)
        if isinstance(route, BaseException):
            raise route
        return route


def base_routes(*, scopes=None, employees=None):
    return {
        "scopes": response({"department_ids": ["d1"], "user_ids": [], "has_more": False} if scopes is None else scopes),
        "departments/d1": response({"department": department()}),
        "departments/d1/children": response(page()),
        "users/find_by_department": response(page([user()] if employees is None else employees)),
    }


def fetch(routes, **options):
    transport = Routes(routes)
    source = modules()[2].FeishuOrganizationSource(
        transport, app_id="directory-app", app_secret="fixture-secret", token_provider=Token(), **options,
    )
    return asyncio.run(source.fetch()), transport


def test_models_are_frozen_and_reject_ambiguous_snapshot_identity():
    model, _, _ = modules()
    employee = model.Employee("u1", "张三", ("d1",), identities=(model.ExternalIdentity("user_id", "u1"),))
    with pytest.raises(dataclasses.FrozenInstanceError):
        employee.display_name = "李四"
    with pytest.raises(ValueError):
        model.DirectorySnapshot((employee, model.Employee("u2", "李四", ("d1",), identities=employee.identities)), (model.Department("d1", "研发部"),), "scope")
    with pytest.raises(ValueError):
        model.DirectorySnapshot((employee,), (), "scope")
    assert model.DirectorySnapshot((), (), "scope").complete is True


@pytest.mark.parametrize("factory,args", [
    ("Employee", ("", "张三")), ("Employee", ("u1", " \n")),
    ("Employee", ("u1", "x" * 513)), ("Department", ("d\x00", "部门")),
    ("ExternalIdentity", ("open_id", "ou_u1", "")),
])
def test_models_bound_and_validate_external_data(factory, args):
    model, _, _ = modules()
    with pytest.raises(ValueError):
        getattr(model, factory)(*args)


def test_provider_builds_complete_named_tree_with_strict_user_ids_and_scoped_aliases():
    snapshot, transport = fetch(base_routes())
    assert snapshot.complete is True
    assert [(item.source_id, item.name, item.parent_id) for item in snapshot.departments] == [("d1", "研发部", "0")]
    employee = snapshot.employees[0]
    assert (employee.source_id, employee.display_name, employee.department_ids, employee.job_title, employee.status) == ("u1", "张三", ("d1",), "工程师", "active")
    assert {(identity.kind, identity.value, identity.app_id) for identity in employee.identities} == {
        ("user_id", "u1", ""), ("open_id", "ou_u1", "directory-app"), ("union_id", "on_u1", ""),
    }
    assert len(snapshot.scope_fingerprint) == 64
    assert snapshot.scope_keys == ("department:d1",)
    assert len(transport.requests) == 4


def test_direct_users_resolve_department_parent_names_and_preserve_invisible_manager():
    routes = base_routes(scopes={"department_ids": [], "user_ids": ["u1"], "has_more": False})
    routes["users/u1"] = response({"user": user(leader_user_id="invisible-manager")})
    routes["departments/d1"] = response({"department": department(parent_department_id="parent")})
    routes["departments/parent"] = response({"department": department("parent", name="总公司")})
    snapshot, _ = fetch(routes)
    assert snapshot.employees[0].manager_id == "invisible-manager"
    assert {item.source_id: item.name for item in snapshot.departments} == {"d1": "研发部", "parent": "总公司"}


def test_provider_omits_empty_scope_arrays_without_making_snapshot_incomplete():
    routes = base_routes(scopes={'user_ids': ['u1'], 'has_more': False})
    routes['users/u1'] = response({'user': user()})
    snapshot, _ = fetch(routes)
    assert snapshot.scope_keys == ('user:u1',)
    assert len(snapshot.employees) == 1


def test_unmaintained_job_title_can_be_absent_without_inventing_a_title():
    value = user()
    del value['job_title']
    routes = base_routes(employees=[value])
    routes['users/u1'] = response({'user': value})
    snapshot, _ = fetch(routes)
    assert snapshot.employees[0].job_title == ''


def test_virtual_root_and_cross_department_users_are_deduplicated():
    routes = base_routes(scopes={"department_ids": ["0"], "user_ids": ["u1"], "has_more": False})
    routes["departments/0/children"] = response(page([department()]))
    routes["users/u1"] = response({"user": user(department_ids=["0", "d1"])})
    routes["users/find_by_department"] = response(page([user(department_ids=["0", "d1"])]))
    snapshot, _ = fetch(routes)
    assert len(snapshot.employees) == 1
    assert snapshot.employees[0].department_ids == ("0", "d1")


def test_employee_detail_fills_fields_omitted_from_list_without_inventing_job_title():
    partial = user()
    del partial["job_title"]
    routes = base_routes(employees=[partial])
    routes["users/u1"] = response({"user": user(job_title="")})
    snapshot, _ = fetch(routes)
    assert snapshot.employees[0].job_title == ""


def test_employee_missing_authorized_fields_rejects_entire_snapshot():
    partial = user()
    del partial["department_ids"]
    routes = base_routes(employees=[partial])
    routes["users/u1"] = response({"user": partial})
    with pytest.raises(modules()[1].SourceError) as error:
        fetch(routes)
    assert error.value.code == "missing_employee_fields"


@pytest.mark.parametrize("wire_status,expected", [
    (None, "unknown"), ({}, "unknown"), ({"is_resigned": True}, "inactive"),
    ({"is_activated": False}, "inactive"), ({"is_frozen": True, "is_activated": True}, "inactive"),
])
def test_employee_status_is_never_invented(wire_status, expected):
    value = user(status=wire_status)
    if wire_status is None:
        del value["status"]
    routes = base_routes(employees=[value])
    routes['users/u1'] = response({'user': value})
    snapshot, _ = fetch(routes)
    assert snapshot.employees[0].status == expected


@pytest.mark.parametrize("bad_page", [
    {"items": [], "has_more": "false"}, {"items": [], "has_more": True},
    {"items": [], "has_more": True, "page_token": ""}, {"items": []},
])
def test_malformed_pagination_cannot_publish_partial_snapshot(bad_page):
    routes = base_routes()
    routes["users/find_by_department"] = response(bad_page)
    with pytest.raises(modules()[1].SourceError) as error:
        fetch(routes)
    assert error.value.code == "invalid_pagination"


def test_repeated_cursor_and_page_limit_reject_partial_snapshot():
    for pages in (
        [response(page(has_more=True, page_token="same"))] * 2,
        [response(page(has_more=True, page_token="one")), response(page(has_more=True, page_token="two"))],
    ):
        routes = base_routes()
        routes["users/find_by_department"] = pages
        with pytest.raises(modules()[1].SourceError) as error:
            fetch(routes, max_pages=2)
        assert error.value.code == "invalid_pagination"


def test_pagination_collects_all_departments_and_normalizes_scope_order():
    routes = base_routes()
    routes["scopes"] = [
        response({"department_ids": ["d1"], "user_ids": [], "has_more": True, "page_token": "next"}),
        response({"department_ids": [], "user_ids": ["u1"], "has_more": False}),
    ]
    routes["users/u1"] = response({"user": user()})
    snapshot, _ = fetch(routes)
    second = base_routes(scopes={"department_ids": ["d1", "d1"], "user_ids": ["u1"], "has_more": False})
    second["users/u1"] = response({"user": user()})
    assert snapshot.scope_fingerprint == fetch(second)[0].scope_fingerprint
    assert snapshot.scope_keys == ("department:d1", "user:u1")


@pytest.mark.parametrize("status,retryable", [(401, False), (403, False), (429, True), (503, True)])
def test_http_failures_are_safe_and_classified(status, retryable):
    routes = base_routes()
    routes["departments/d1/children"] = response(status=status, message="private-provider-secret")
    with pytest.raises(modules()[1].SourceError) as error:
        fetch(routes)
    assert error.value.retryable is retryable
    assert "private-provider-secret" not in str(error.value)
    assert "fixture-secret" not in str(error.value)


def test_transport_failure_does_not_escape_provider_message():
    routes = base_routes()
    routes["scopes"] = RuntimeError("private-provider-secret")
    with pytest.raises(modules()[1].SourceError) as error:
        fetch(routes)
    assert error.value.code == "transport_error"
    assert error.value.retryable is True
    assert "private-provider-secret" not in str(error.value)
    assert error.value.__cause__ is None


def test_cancellation_is_not_swallowed():
    routes = base_routes()
    routes["scopes"] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        fetch(routes)


def test_missing_department_name_is_a_permission_failure():
    routes = base_routes()
    incomplete = department()
    del incomplete["name"]
    routes["departments/d1"] = response({"department": incomplete})
    with pytest.raises(modules()[1].SourceError) as error:
        fetch(routes)
    assert error.value.code == "insufficient_permissions"


def test_group_scope_is_rejected_until_it_can_be_expanded_completely():
    routes = base_routes(scopes={"department_ids": [], "user_ids": [], "group_ids": ["group"], "has_more": False})
    with pytest.raises(modules()[1].SourceError) as error:
        fetch(routes)
    assert error.value.code == "unsupported_scope"


def test_shared_cached_token_provider_is_used_and_expired_token_is_refreshed_once():
    class RefreshTransport(FeishuHttpTransport):
        def __init__(self):
            self.token_calls = 0
            self.scope_calls = 0

        async def send(self, request):
            if request.method == "POST":
                self.token_calls += 1
                assert request.url.endswith("/auth/v3/tenant_access_token/internal")
                return FeishuHttpResponse(200, json.dumps({"code": 0, "tenant_access_token": "token-" + str(self.token_calls), "expire": 7200}).encode())
            self.scope_calls += 1
            if self.scope_calls == 1:
                return response(code=99991663, message="do-not-publish")
            return response({"department_ids": [], "user_ids": [], "has_more": False})

    transport = RefreshTransport()
    provider = modules()[2].FeishuOrganizationSource(transport, app_id="directory-app", app_secret="fixture-secret")
    snapshot = asyncio.run(provider.fetch())
    assert snapshot.employees == ()
    assert transport.token_calls == 2
    assert transport.scope_calls == 2


def test_snapshot_scope_keys_are_strict_unique_and_allow_legacy_default():
    model, _, _ = modules()
    assert model.DirectorySnapshot((), (), "scope").scope_keys == ()
    assert model.DirectorySnapshot((), (), "scope", scope_keys=("department:0",)).scope_keys == ("department:0",)
    with pytest.raises(ValueError):
        model.DirectorySnapshot((), (), "scope", scope_keys=("user:u1", "user:u1"))
    with pytest.raises(ValueError):
        model.DirectorySnapshot((), (), "scope", scope_keys=["user:u1"])


@pytest.mark.parametrize("options", [{"status": []}, {"department_ids": ["d1"]}, {"identities": []}])
def test_employee_validation_rejects_mutable_or_mistyped_fields(options):
    model, _, _ = modules()
    with pytest.raises(ValueError):
        model.Employee("u1", "张三", **options)


def test_snapshot_rejects_visible_department_cycles():
    model, _, _ = modules()
    with pytest.raises(ValueError):
        model.DirectorySnapshot((), (model.Department("one", "一部", "two"), model.Department("two", "二部", "one")), "scope")


def test_cross_department_conflicting_records_reject_the_snapshot():
    routes = base_routes(employees=[user(), user(name="已改变")])
    with pytest.raises(modules()[1].SourceError) as error:
        fetch(routes)
    assert error.value.code == "directory_changed"
    assert error.value.retryable is True


def test_source_has_bounded_concurrency_for_many_direct_users():
    async def check():
        current = 0
        maximum = 0

        class ConcurrentRoutes(Routes):
            async def send(self, request):
                nonlocal current, maximum
                current += 1
                maximum = max(maximum, current)
                try:
                    await asyncio.sleep(0)
                    return await super().send(request)
                finally:
                    current -= 1

        users = ["u" + str(index) for index in range(12)]
        routes = base_routes(scopes={"department_ids": [], "user_ids": users, "has_more": False})
        routes.update({"users/" + identifier: response({"user": user(identifier)}) for identifier in users})
        transport = ConcurrentRoutes(routes)
        source = modules()[2].FeishuOrganizationSource(transport, app_id="directory-app", app_secret="fixture-secret", token_provider=Token(), max_concurrency=3)
        snapshot = await source.fetch()
        assert len(snapshot.employees) == 12
        assert maximum == 3

    asyncio.run(check())


def test_token_expiry_cannot_trigger_an_unbounded_refresh_loop():
    class ExpiredToken(Token):
        def __init__(self):
            self.invalidations = 0

        async def invalidate(self, token):
            self.invalidations += 1

    tokens = ExpiredToken()
    transport = Routes({"scopes": response(code=99991663, message="private-provider-secret")})
    source = modules()[2].FeishuOrganizationSource(transport, app_id="directory-app", app_secret="fixture-secret", token_provider=tokens)
    with pytest.raises(modules()[1].SourceError) as error:
        asyncio.run(source.fetch())
    assert error.value.code == "authentication_failed"
    assert tokens.invalidations == 1
    assert len(transport.requests) == 2


def test_absent_enterprise_user_id_cannot_be_replaced_by_app_open_id():
    value = user()
    del value["user_id"]
    with pytest.raises(modules()[1].SourceError) as error:
        fetch(base_routes(employees=[value]))
    assert error.value.code == "missing_employee_fields"


def test_child_department_must_belong_to_the_requested_parent():
    routes = base_routes()
    routes["departments/d1/children"] = response(page([department("d2", parent_department_id="wrong-parent")]))
    routes["departments/d2/children"] = response(page())
    with pytest.raises(modules()[1].SourceError) as error:
        fetch(routes)
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize("field", ["open_id", "union_id", "leader_user_id"])
def test_optional_employee_fields_do_not_hide_invalid_false_values(field):
    with pytest.raises(modules()[1].SourceError) as error:
        fetch(base_routes(employees=[user(**{field: 0})]))
    assert error.value.code == "missing_employee_fields"


def test_optional_department_leader_does_not_hide_invalid_false_values():
    routes = base_routes()
    routes["departments/d1"] = response({"department": department(leader_user_id=0)})
    with pytest.raises(modules()[1].SourceError) as error:
        fetch(routes)
    assert error.value.code == "insufficient_permissions"


def test_invalid_json_and_business_code_never_publish_a_snapshot():
    for invalid in (FeishuHttpResponse(200, b"not-json-private-provider-secret"), FeishuHttpResponse(200, b'{"code":true,"data":{}}')):
        routes = base_routes()
        routes["scopes"] = invalid
        with pytest.raises(modules()[1].SourceError) as error:
            fetch(routes)
        assert error.value.code == "invalid_response"
        assert "private-provider-secret" not in str(error.value)

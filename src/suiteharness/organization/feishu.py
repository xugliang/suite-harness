"""Read-only Feishu contact source using the shared channel HTTP boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any, TypeVar
from urllib.parse import quote, urlencode

from pydantic import SecretStr

from suiteharness.channels.feishu.client import (
    CachedTenantAccessTokenProvider,
    FeishuHttpRequest,
    FeishuHttpTransport,
    TenantAccessTokenProvider,
)
from suiteharness.channels.feishu.models import FeishuApiError

from .models import Department, DirectorySnapshot, Employee, ExternalIdentity, _text
from .source import OrganizationSource, SourceError

_T = TypeVar("_T")
_R = TypeVar("_R")
_BASE = "https://open.feishu.cn/open-apis/contact/v3/"
_TOKEN_EXPIRED = frozenset({99991661, 99991663, 99991668})
_PERMISSION = frozenset({99991672, 99991679, 99991680, 99991681, 99991682, 41050, 41051, 43001, 43002})
_RATE_LIMIT = frozenset({99991400, 99991401, 99991402})


def _failure(code: str, *, retryable: bool = False) -> SourceError:
    return SourceError(f"Feishu organization source failed ({code})", code=code, retryable=retryable)


def _ids(value: object, *, code: str = "invalid_response") -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 100_000:
        raise _failure(code)
    try:
        for item in value:
            _text(item, "provider ID")
        return tuple(sorted(set(value)))
    except (TypeError, ValueError):
        raise _failure(code) from None


def _optional_provider_id(value: object) -> str | None:
    if value is None or value == "":
        return None
    _text(value, "provider optional ID")
    return value


class FeishuOrganizationSource(OrganizationSource):
    """Fetch only the app's authorized contact scope, without partial publication.

    ``user_id`` is mandatory. ``open_id`` is app scoped, and ``union_id`` is
    meaningful only within the same Feishu app provider. Group scopes are
    rejected until an implementation can expand them completely.
    """

    def __init__(
        self,
        transport: FeishuHttpTransport,
        *,
        app_id: str,
        app_secret: str | SecretStr,
        token_provider: TenantAccessTokenProvider | None = None,
        max_pages: int = 1000,
        max_concurrency: int = 8,
    ) -> None:
        _text(app_id, "Feishu app ID")
        if type(max_pages) is not int or not 1 <= max_pages <= 1000:
            raise ValueError("max_pages must be between 1 and 1000")
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 32:
            raise ValueError("max_concurrency must be between 1 and 32")
        self._transport = transport
        self._app_id = app_id
        self._tokens = token_provider or CachedTenantAccessTokenProvider(
            transport, app_id=app_id, app_secret=app_secret,
        )
        self._max_pages = max_pages
        self._max_concurrency = max_concurrency
        self._refresh_lock = asyncio.Lock()

    async def _token(self) -> str:
        try:
            token = await self._tokens.get_token()
            _text(token, "tenant access token", maximum=4096)
            return token
        except FeishuApiError as exc:
            raise _failure("token_error", retryable=bool(exc.retryable)) from None
        except Exception:
            raise _failure("token_error", retryable=True) from None

    async def _get(self, path: str, **params: str) -> dict[str, Any]:
        query = urlencode({"user_id_type": "user_id", "department_id_type": "department_id", **params})
        token = await self._token()
        for attempt in range(2):
            try:
                response = await self._transport.send(FeishuHttpRequest(
                    method="GET", url=f"{_BASE}{path}?{query}",
                    headers={"Authorization": f"Bearer {token}"},
                ))
            except Exception:
                raise _failure("transport_error", retryable=True) from None
            if response.status_code in {401, 403}:
                raise _failure("insufficient_permissions")
            if not 200 <= response.status_code < 300:
                raise _failure("http_error", retryable=response.status_code == 429 or response.status_code >= 500)
            try:
                payload = json.loads(response.body)
            except (ValueError, TypeError, UnicodeError):
                raise _failure("invalid_response") from None
            if not isinstance(payload, dict) or type(payload.get("code")) is not int:
                raise _failure("invalid_response")
            code = payload["code"]
            if code in _TOKEN_EXPIRED:
                invalidate = getattr(self._tokens, "invalidate", None)
                if attempt or not callable(invalidate):
                    raise _failure("authentication_failed")
                try:
                    async with self._refresh_lock:
                        await invalidate(token)
                        token = await self._token()
                except SourceError:
                    raise
                except Exception:
                    raise _failure("token_error") from None
                continue
            if code != 0:
                raise _failure("insufficient_permissions" if code in _PERMISSION else "provider_error", retryable=code in _RATE_LIMIT)
            data = payload.get("data")
            if not isinstance(data, dict):
                raise _failure("invalid_response")
            return data
        raise _failure("authentication_failed")  # pragma: no cover

    async def _pages(self, path: str, **params: str) -> AsyncIterator[dict[str, Any]]:
        cursor = ""
        seen: set[str] = set()
        for _ in range(self._max_pages):
            data = await self._get(path, page_size="50", **params, **({"page_token": cursor} if cursor else {}))
            if type(data.get("has_more")) is not bool:
                raise _failure("invalid_pagination")
            yield data
            if not data["has_more"]:
                return
            following = data.get("page_token")
            try:
                _text(following, "pagination cursor", maximum=4096)
            except ValueError:
                raise _failure("invalid_pagination") from None
            if following in seen:
                raise _failure("invalid_pagination")
            seen.add(following)
            cursor = following
        raise _failure("invalid_pagination")

    async def _map(self, values: Iterable[_T], function: Callable[[_T], Awaitable[_R]]) -> list[_R]:
        """Create at most the configured number of requests/tasks at a time."""
        iterator = iter(values)
        output: list[_R] = []
        while True:
            batch = []
            for _ in range(self._max_concurrency):
                try:
                    batch.append(next(iterator))
                except StopIteration:
                    break
            if not batch:
                return output
            results = await asyncio.gather(*(function(value) for value in batch), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
                output.append(result)

    @staticmethod
    def _department(value: object) -> Department:
        if not isinstance(value, dict) or not {"department_id", "name", "parent_department_id"} <= value.keys():
            raise _failure("insufficient_permissions")
        try:
            return Department(
                source_id=value["department_id"], name=value["name"],
                parent_id=value["parent_department_id"], leader_id=_optional_provider_id(value.get("leader_user_id")),
            )
        except (ValueError, TypeError):
            raise _failure("insufficient_permissions") from None

    async def _department_detail(self, department_id: str) -> Department:
        data = await self._get(f"departments/{quote(department_id, safe='')}")
        department = self._department(data.get("department"))
        if department.source_id != department_id:
            raise _failure("invalid_response")
        return department

    async def _children(self, department_id: str) -> list[Department]:
        result: list[Department] = []
        async for data in self._pages(f"departments/{quote(department_id, safe='')}/children", fetch_child="false"):
            items = data.get("items")
            if not isinstance(items, list):
                raise _failure("invalid_response")
            for item in items:
                department = self._department(item)
                if department.parent_id != department_id:
                    raise _failure("invalid_response")
                result.append(department)
            if len(result) > 10_000:
                raise _failure("directory_limit")
        return result

    async def _department_users(self, department_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        async for data in self._pages("users/find_by_department", department_id=department_id):
            items = data.get("items")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise _failure("invalid_response")
            result.extend(items)
            if len(result) > 100_000:
                raise _failure("directory_limit")
        return result

    async def _user_detail(self, user_id: str) -> dict[str, Any]:
        data = await self._get(f"users/{quote(user_id, safe='')}")
        value = data.get("user")
        if not isinstance(value, dict) or value.get("user_id") != user_id:
            raise _failure("missing_employee_fields")
        return value

    @staticmethod
    def _status(value: object) -> str:
        if value is None:
            return "unknown"
        if not isinstance(value, dict):
            raise _failure("invalid_response")
        fields = ("is_frozen", "is_resigned", "is_activated", "is_exited", "is_unjoin")
        if any(key in value and type(value[key]) is not bool for key in fields):
            raise _failure("invalid_response")
        if any(value.get(key) is True for key in ("is_frozen", "is_resigned", "is_exited", "is_unjoin")):
            return "inactive"
        if "is_activated" in value:
            return "active" if value["is_activated"] else "inactive"
        return "unknown"

    async def _employee(self, value: dict[str, Any]) -> Employee:
        try:
            _text(value.get("user_id"), "provider user ID")
        except ValueError:
            raise _failure("missing_employee_fields") from None
        if not {"name", "department_ids", "job_title", "status"} <= value.keys():
            value = await self._user_detail(value["user_id"])
        if not {"name", "department_ids"} <= value.keys():
            raise _failure("missing_employee_fields")
        identities = [ExternalIdentity("user_id", value["user_id"])]
        try:
            for kind in ("open_id", "union_id"):
                identity_value = _optional_provider_id(value.get(kind))
                if identity_value is not None:
                    identities.append(ExternalIdentity(kind, identity_value, self._app_id if kind == "open_id" else ""))
            return Employee(
                source_id=value["user_id"], display_name=value["name"],
                department_ids=_ids(value["department_ids"], code="missing_employee_fields"),
                job_title=value["job_title"] if value.get("job_title") is not None else "", status=self._status(value.get("status")),
                manager_id=_optional_provider_id(value.get("leader_user_id")), identities=tuple(identities),
            )
        except (ValueError, TypeError):
            raise _failure("missing_employee_fields") from None

    async def fetch(self) -> DirectorySnapshot:
        departments_scope: set[str] = set()
        users_scope: set[str] = set()
        async for data in self._pages("scopes"):
            departments_scope.update(_ids(data.get("department_ids", [])))
            users_scope.update(_ids(data.get("user_ids", [])))
            if _ids(data.get("group_ids", [])):
                raise _failure("unsupported_scope")
        scope_keys = tuple(sorted(
            [f"department:{value}" for value in departments_scope]
            + [f"user:{value}" for value in users_scope]
        ))
        fingerprint = hashlib.sha256(json.dumps(scope_keys, separators=(",", ":")).encode()).hexdigest()
        departments: dict[str, Department] = {}

        def add_department(value: Department) -> None:
            previous = departments.get(value.source_id)
            if previous is not None and previous != value:
                raise _failure("directory_changed", retryable=True)
            departments[value.source_id] = value
            if len(departments) > 10_000:
                raise _failure("directory_limit")

        for value in await self._map(sorted(departments_scope - {"0"}), self._department_detail):
            add_department(value)
        pending = sorted(departments_scope)
        expanded: set[str] = set()
        while pending:
            expanded.update(pending)
            batches = await self._map(pending, self._children)
            following: set[str] = set()
            for batch in batches:
                for value in batch:
                    add_department(value)
                    if value.source_id not in expanded:
                        following.add(value.source_id)
            pending = sorted(following)
        raw_users: list[dict[str, Any]] = []
        for batch in await self._map(sorted(expanded), self._department_users):
            raw_users.extend(batch)
            if len(raw_users) > 100_000:
                raise _failure("directory_limit")
        raw_users.extend(await self._map(sorted(users_scope), self._user_detail))
        employees: dict[str, Employee] = {}
        for employee in await self._map(raw_users, self._employee):
            previous = employees.get(employee.source_id)
            if previous is not None and previous != employee:
                raise _failure("directory_changed", retryable=True)
            employees[employee.source_id] = employee
        # Direct user scopes can reference departments absent from department scopes.
        pending_ids = {item for employee in employees.values() for item in employee.department_ids} - departments.keys() - {"0"}
        while pending_ids:
            following = set()
            for value in await self._map(sorted(pending_ids), self._department_detail):
                add_department(value)
                if value.parent_id and value.parent_id != "0" and value.parent_id not in departments:
                    following.add(value.parent_id)
            pending_ids = following
        try:
            return DirectorySnapshot(
                employees=tuple(employees[key] for key in sorted(employees)),
                departments=tuple(departments[key] for key in sorted(departments)),
                scope_fingerprint=fingerprint,
                scope_keys=scope_keys,
            )
        except ValueError:
            raise _failure("invalid_snapshot") from None


__all__ = ["FeishuOrganizationSource"]

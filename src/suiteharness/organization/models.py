"""Immutable, validated organization records independent of any provider."""

from __future__ import annotations

from dataclasses import dataclass


def _text(value: object, field: str, *, maximum: int = 256, blank: bool = False) -> None:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not blank and not value.strip())
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"Invalid organization {field}")


def _optional_id(value: object, field: str) -> None:
    if value is not None:
        _text(value, field)


@dataclass(frozen=True, slots=True)
class Department:
    source_id: str
    name: str
    parent_id: str | None = None
    leader_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.source_id, "department ID")
        _text(self.name, "department name", maximum=512)
        _optional_id(self.parent_id, "parent ID")
        _optional_id(self.leader_id, "department leader ID")
        if self.parent_id == self.source_id and self.source_id != "0":
            raise ValueError("Organization department cannot parent itself")


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    kind: str
    value: str
    app_id: str = ""

    def __post_init__(self) -> None:
        _text(self.kind, "identity kind", maximum=64)
        _text(self.value, "identity value")
        _text(self.app_id, "identity app ID", blank=True)
        if self.kind == "open_id" and not self.app_id:
            raise ValueError("Organization open_id requires its app ID")


@dataclass(frozen=True, slots=True)
class Employee:
    source_id: str
    display_name: str
    department_ids: tuple[str, ...] = ()
    job_title: str = ""
    status: str = "unknown"
    manager_id: str | None = None
    identities: tuple[ExternalIdentity, ...] = ()

    def __post_init__(self) -> None:
        _text(self.source_id, "employee ID")
        _text(self.display_name, "employee name", maximum=512)
        _text(self.job_title, "job title", maximum=512, blank=True)
        _optional_id(self.manager_id, "manager ID")
        if not isinstance(self.status, str) or self.status not in {"active", "inactive", "unknown"}:
            raise ValueError("Invalid organization employee status")
        if not isinstance(self.department_ids, tuple) or len(self.department_ids) > 1000:
            raise ValueError("Invalid organization employee departments")
        for department_id in self.department_ids:
            _text(department_id, "employee department ID")
        if len(set(self.department_ids)) != len(self.department_ids):
            raise ValueError("Duplicate organization employee department")
        if not isinstance(self.identities, tuple) or len(self.identities) > 32:
            raise ValueError("Invalid organization employee identities")
        if any(not isinstance(identity, ExternalIdentity) for identity in self.identities):
            raise ValueError("Invalid organization employee identity")
        keys = [(identity.kind, identity.value, identity.app_id) for identity in self.identities]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate organization employee identity")


@dataclass(frozen=True, slots=True)
class DirectorySnapshot:
    employees: tuple[Employee, ...]
    departments: tuple[Department, ...]
    scope_fingerprint: str
    complete: bool = True
    scope_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.scope_fingerprint, "scope fingerprint", maximum=256)
        if type(self.complete) is not bool:
            raise ValueError("Invalid organization snapshot completeness")
        if not isinstance(self.scope_keys, tuple) or len(self.scope_keys) > 100_000:
            raise ValueError("Invalid organization snapshot scope keys")
        for scope_key in self.scope_keys:
            _text(scope_key, "scope key", maximum=512)
        if len(set(self.scope_keys)) != len(self.scope_keys):
            raise ValueError("Duplicate organization snapshot scope key")
        if not isinstance(self.employees, tuple) or len(self.employees) > 100_000:
            raise ValueError("Invalid organization snapshot employees")
        if not isinstance(self.departments, tuple) or len(self.departments) > 10_000:
            raise ValueError("Invalid organization snapshot departments")
        if any(not isinstance(item, Employee) for item in self.employees):
            raise ValueError("Invalid organization snapshot employee")
        if any(not isinstance(item, Department) for item in self.departments):
            raise ValueError("Invalid organization snapshot department")
        department_map = {item.source_id: item for item in self.departments}
        if len(department_map) != len(self.departments):
            raise ValueError("Duplicate organization department ID")
        if len({item.source_id for item in self.employees}) != len(self.employees):
            raise ValueError("Duplicate organization employee ID")
        identities: set[tuple[str, str, str]] = set()
        for employee in self.employees:
            if any(item != "0" and item not in department_map for item in employee.department_ids):
                raise ValueError("Organization employee references an unknown department")
            for identity in employee.identities:
                key = (identity.kind, identity.value, identity.app_id)
                if key in identities:
                    raise ValueError("Organization identity belongs to multiple employees")
                identities.add(key)
        # Scope roots may point to invisible parents; visible cycles are invalid.
        visited: set[str] = set()
        for department_id in department_map:
            path: set[str] = set()
            current: str | None = department_id
            while current in department_map and current != "0" and current not in visited:
                if current in path:
                    raise ValueError("Organization department hierarchy contains a cycle")
                path.add(current)
                current = department_map[current].parent_id
            visited.update(path)


__all__ = ["Department", "DirectorySnapshot", "Employee", "ExternalIdentity"]

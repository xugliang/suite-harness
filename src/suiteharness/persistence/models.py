"""Validated scope and record vocabulary for durable non-secret state."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NAMESPACE = re.compile(r"^[a-z][a-z0-9]*(?:[._:/-][a-z0-9]+)*$")


def canonical_json(value: JsonValue) -> str:
    """Produce deterministic UTF-8 JSON and reject non-finite floats."""

    def validate(item: object) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("persistent JSON cannot contain NaN or infinity")
        if isinstance(item, dict):
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)

    validate(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utc_now() -> datetime:
    return datetime.now(UTC)


def _identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProductStateKey(_FrozenModel):
    """Complete company-product key for plugin and transport checkpoints."""

    tenant_id: str
    product_id: str
    namespace: str
    key: str

    @field_validator("tenant_id", "product_id")
    @classmethod
    def validate_scope_ids(cls, value: str, info: object) -> str:
        return _identifier(value, getattr(info, "field_name", "identifier"))

    @field_validator("namespace")
    @classmethod
    def validate_namespace(cls, value: str) -> str:
        if len(value) > 128 or not _NAMESPACE.fullmatch(value):
            raise ValueError("invalid state namespace")
        segments = re.split(r"[._:/-]", value)
        if any(segment in {"approval", "approvals"} for segment in segments):
            raise ValueError("approval state must not use the generic persistence store")
        return value

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not value or len(value) > 512 or "\x00" in value:
            raise ValueError("invalid state key")
        return value


class StateRecord(_FrozenModel):
    state_key: ProductStateKey
    value: JsonValue
    revision: int = Field(ge=1)
    digest: str
    created_at: datetime
    updated_at: datetime

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: JsonValue) -> JsonValue:
        canonical_json(value)
        return value

    @field_validator("digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("invalid state digest")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def validate_time_window(self) -> StateRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        return self


class StateWriteResult(_FrozenModel):
    record: StateRecord
    replayed: bool = False


class EventScopeKind(str, Enum):
    DEPLOYMENT = "deployment"
    PRODUCT = "product"


class ChannelEventScope(_FrozenModel):
    """An explicit deployment or tenant/product channel deduplication scope."""

    kind: EventScopeKind
    channel_id: str
    deployment_id: str | None = None
    tenant_id: str | None = None
    product_id: str | None = None

    @field_validator("channel_id")
    @classmethod
    def validate_channel_id(cls, value: str) -> str:
        return _identifier(value, "channel_id")

    @field_validator("deployment_id", "tenant_id", "product_id")
    @classmethod
    def validate_optional_ids(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _identifier(value, getattr(info, "field_name", "identifier"))

    @model_validator(mode="after")
    def validate_shape(self) -> ChannelEventScope:
        if self.kind is EventScopeKind.DEPLOYMENT:
            if self.deployment_id is None or self.tenant_id is not None or self.product_id is not None:
                raise ValueError("deployment event scope requires only deployment_id")
        elif (
            self.deployment_id is not None
            or self.tenant_id is None
            or self.product_id is None
        ):
            raise ValueError("product event scope requires only tenant_id and product_id")
        return self

    @classmethod
    def deployment(cls, deployment_id: str, channel_id: str) -> ChannelEventScope:
        return cls(
            kind=EventScopeKind.DEPLOYMENT,
            deployment_id=deployment_id,
            channel_id=channel_id,
        )

    @classmethod
    def product(cls, tenant_id: str, product_id: str, channel_id: str) -> ChannelEventScope:
        return cls(
            kind=EventScopeKind.PRODUCT,
            tenant_id=tenant_id,
            product_id=product_id,
            channel_id=channel_id,
        )

    def storage_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.kind.value,
            self.deployment_id or "",
            self.tenant_id or "",
            self.product_id or "",
            self.channel_id,
        )


__all__ = [
    "ChannelEventScope",
    "EventScopeKind",
    "ProductStateKey",
    "StateRecord",
    "StateWriteResult",
    "canonical_json",
    "utc_now",
]

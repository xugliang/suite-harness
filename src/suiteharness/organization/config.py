"""Opt-in directory settings and private credential references."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

PUBLIC_FIELDS = frozenset({'employee_id', 'display_name', 'department_ids', 'department_names', 'job_title', 'status', 'updated_at', 'source_updated_at', 'revision'})


class ClientPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    credentials_ref: str = Field(min_length=1, max_length=128)
    allow_directory_search: bool = True
    fields: tuple[str, ...] = tuple(sorted(PUBLIC_FIELDS))
    department_fields: tuple[str, ...] = ('department_id', 'name', 'parent_id')

    @field_validator('fields')
    @classmethod
    def fields_allowed(cls, values):
        if not values or not set(values).issubset(PUBLIC_FIELDS):
            raise ValueError('unsupported directory fields')
        return values

    @field_validator('department_fields')
    @classmethod
    def department_fields_allowed(cls, values):
        if not values or not set(values).issubset({'department_id', 'name', 'parent_id'}):
            raise ValueError('unsupported department fields')
        return values


class DirectoryConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    enabled: bool = False
    provider: Literal['feishu'] = 'feishu'
    app_id: str | None = Field(default=None, max_length=256)
    credentials_ref: str = 'organization_feishu'
    dsn_secret_ref: str = 'organization_database'
    database_name: str = 'organization_directory'
    clients: dict[str, ClientPolicy] = Field(default_factory=dict)
    refresh_seconds: int = Field(default=86400, ge=300, le=86400)
    consumer_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    check_interval_seconds: int = Field(default=30, ge=1, le=300)
    listen_host: str = '127.0.0.1'
    listen_port: int = Field(default=18797, ge=1024, le=65535)

    @field_validator('database_name')
    @classmethod
    def database_allowed(cls, value):
        import re
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value):
            raise ValueError('invalid directory database name')
        return value

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps({'provider': self.provider, 'app_id': self.app_id, 'database': self.database_name, 'format': 1}, sort_keys=True).encode()).hexdigest()


def _reveal(value):
    return value.get_secret_value() if isinstance(value, SecretStr) else value


def load_directory_config(config: DirectoryConfig, secrets: dict):
    """Missing app ID is deliberately inert, even in an otherwise enabled file."""
    if not config.enabled or not config.app_id:
        return None
    try:
        app_secret = _reveal(secrets['feishu'][config.credentials_ref]['app_secret'])
        dsn = _reveal(secrets['databases'][config.dsn_secret_ref]['dsn'])
        tokens = {product: _reveal(secrets['services'][policy.credentials_ref]['token']) for product, policy in config.clients.items()}
        if not isinstance(app_secret, str) or not app_secret.strip() or not isinstance(dsn, str) or not dsn:
            raise ValueError()
        if any(not isinstance(token, str) or len(token) < 32 for token in tokens.values()) or len(set(tokens.values())) != len(tokens):
            raise ValueError()
        if any(not isinstance(product, str) or not product or len(product) > 128 for product in tokens):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError('directory credentials missing or invalid') from None
    return {'app_secret': SecretStr(app_secret), 'dsn': SecretStr(dsn), 'tokens': {p: SecretStr(t) for p, t in tokens.items()}}

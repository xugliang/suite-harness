"""Strict configuration vocabulary for a company-hosted SuiteHarness deployment.

Configuration describes a server installation.  User supplied environment
variables and personal-machine deployment modes are intentionally absent from
the model, so applications cannot accidentally make them part of the public
configuration contract.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)

from suiteharness.mcp.models import McpServerConfig, McpToolSecurityOverride
from suiteharness.plugins.models import PluginSourceDeclaration
from suiteharness.runtime.customer_bundle import CustomerBundleManifest
from suiteharness.sessions.models import SessionStoreLimits

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SECRET_REF = re.compile(r"^[a-z][a-z0-9]*(?:[._/-][a-z0-9]+)*$")
_DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


def _identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _secret_ref(value: str, label: str) -> str:
    if not _SECRET_REF.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _absolute_path(value: Path, label: str) -> Path:
    if not value.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return value


def _relative_workspace_path(value: str) -> str:
    """Validate one portable path relative to a workspace space.

    Both slash forms are treated as separators so a configuration authored on
    Linux cannot turn into an escaping path when deployed on Windows (or vice
    versa).  A dot denotes the selected space root.
    """

    if not value or "\x00" in value:
        raise ValueError("workspace path must not be empty or contain NUL")
    portable = value.replace("\\", "/")
    if portable == ".":
        return value
    if portable.startswith("/") or portable.startswith("//"):
        raise ValueError("workspace path must be relative")
    if re.match(r"^[A-Za-z]:", portable):
        raise ValueError("workspace path must not contain a drive prefix")
    raw_parts = portable.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("workspace path must be normalized and must not contain '..'")
    parts = PurePosixPath(portable).parts
    if tuple(raw_parts) != parts:
        raise ValueError("workspace path must be normalized")
    return value


class _FrozenConfig(BaseModel):
    # YAML has no native Path, tuple or frozenset nodes. Pydantic performs only
    # those structural conversions; every security-sensitive value still has
    # an explicit validator below.
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeploymentConfig(_FrozenConfig):
    """Identity and lifecycle of one company-owned server installation."""

    mode: Literal["server"] = "server"
    environment: Literal["development", "production"] = "production"
    instance_id: str
    tenant_id: str

    @field_validator("instance_id", "tenant_id")
    @classmethod
    def validate_ids(cls, value: str, info: object) -> str:
        return _identifier(value, getattr(info, "field_name", "identifier"))


class ServerConfig(_FrozenConfig):
    """Company ASGI listener and bounded process lifecycle settings."""

    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65_535)
    max_concurrent_connections: int = Field(default=1000, ge=1, le=100_000)
    backlog: int = Field(default=2048, ge=1, le=65_535)
    startup_timeout_seconds: float = Field(default=300.0, gt=0.0, le=3600.0)
    shutdown_timeout_seconds: float = Field(default=60.0, gt=0.0, le=600.0)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if (
            not value
            or value != value.strip()
            or len(value) > 253
            or not value.isascii()
            or any(character.isspace() for character in value)
            or any(character in value for character in ("/", "\\", "\x00"))
        ):
            raise ValueError("server host must be a bounded IP address or DNS name")
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            labels = value.split(".")
            if any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in labels
            ):
                raise ValueError(
                    "server host must be a bounded IP address or DNS name"
                ) from None
            return value.casefold()


class WorkspaceRootConfig(_FrozenConfig):
    """A configured directory relative to a product or tenant shared space."""

    space: Literal["product", "shared"] = "product"
    path: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _relative_workspace_path(value)


class WorkspaceConfig(_FrozenConfig):
    root: Path
    shared_enabled: bool = False
    shared_access_by_product: dict[
        str, Literal["read_only", "read_write"]
    ] = Field(default_factory=dict)

    @field_validator("root")
    @classmethod
    def validate_root(cls, value: Path) -> Path:
        return _absolute_path(value, "workspace.root")

    @field_validator("shared_access_by_product")
    @classmethod
    def validate_shared_access(
        cls,
        value: dict[str, Literal["read_only", "read_write"]],
    ) -> dict[str, Literal["read_only", "read_write"]]:
        return {
            _identifier(product_id, "shared workspace product_id"): access
            for product_id, access in value.items()
        }

    @model_validator(mode="after")
    def validate_shared_enabled(self) -> WorkspaceConfig:
        if self.shared_access_by_product and not self.shared_enabled:
            raise ValueError("shared workspace access requires shared_enabled=true")
        return self


class StorageConfig(_FrozenConfig):
    """Durable server state, deliberately separate from product workspaces."""

    root: Path
    sessions_database: str = "sessions.sqlite3"
    audit_database: str = "audit.sqlite3"
    runtime_database: str = "runtime.sqlite3"
    session_limits: SessionStoreLimits = Field(default_factory=SessionStoreLimits)

    @field_validator("root")
    @classmethod
    def validate_root(cls, value: Path) -> Path:
        return _absolute_path(value, "storage.root")

    @field_validator("sessions_database", "audit_database", "runtime_database")
    @classmethod
    def validate_database_path(cls, value: str) -> str:
        normalized = _relative_workspace_path(value)
        if normalized == ".":
            raise ValueError("database path must name a file")
        return normalized

    @model_validator(mode="after")
    def distinct_databases(self) -> StorageConfig:
        databases = (
            self.sessions_database,
            self.audit_database,
            self.runtime_database,
        )
        if len(databases) != len(set(databases)):
            raise ValueError("session, audit and runtime databases must use different files")
        return self

    def sessions_path(self) -> Path:
        return self.root.joinpath(*PurePosixPath(self.sessions_database).parts)

    def audit_path(self) -> Path:
        return self.root.joinpath(*PurePosixPath(self.audit_database).parts)

    def runtime_path(self) -> Path:
        return self.root.joinpath(*PurePosixPath(self.runtime_database).parts)


class SandboxLimitsConfig(_FrozenConfig):
    cpu_count: float = Field(default=1.0, gt=0.0, le=64.0)
    memory_mb: int = Field(default=512, ge=64, le=262_144)
    pids: int = Field(default=128, ge=16, le=32_768)
    timeout_seconds: float = Field(default=120.0, gt=0.0, le=86_400.0)
    output_bytes: int = Field(default=1_048_576, ge=1_024, le=1_073_741_824)
    tmpfs_mb: int = Field(default=128, ge=16, le=65_536)


class SandboxNetworkConfig(_FrozenConfig):
    default: Literal["none"] = "none"
    egress_profiles: dict[str, str] = Field(default_factory=dict)
    allowed_profiles_by_product: dict[str, frozenset[str]] = Field(default_factory=dict)

    @field_validator("egress_profiles")
    @classmethod
    def validate_profiles(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            _identifier(profile, "egress profile"): _identifier(network, "Docker network")
            for profile, network in value.items()
        }

    @field_validator("allowed_profiles_by_product")
    @classmethod
    def validate_product_profiles(
        cls,
        value: dict[str, frozenset[str]],
    ) -> dict[str, frozenset[str]]:
        return {
            _identifier(product_id, "sandbox network product_id"): frozenset(
                _identifier(profile, "sandbox product egress profile")
                for profile in profiles
            )
            for product_id, profiles in value.items()
        }

    @model_validator(mode="after")
    def validate_allowed_profiles(self) -> SandboxNetworkConfig:
        configured = set(self.egress_profiles)
        unknown = {
            profile
            for profiles in self.allowed_profiles_by_product.values()
            for profile in profiles
            if profile not in configured
        }
        if unknown:
            raise ValueError(
                f"product Bash access references unknown egress profiles: {sorted(unknown)!r}"
            )
        return self


class DockerSandboxConfig(_FrozenConfig):
    backend: Literal["docker"] = "docker"
    required: Literal[True] = True
    binary: str = "docker"
    image: str
    limits: SandboxLimitsConfig = Field(default_factory=SandboxLimitsConfig)
    network: SandboxNetworkConfig = Field(default_factory=SandboxNetworkConfig)

    @field_validator("binary")
    @classmethod
    def validate_binary(cls, value: str) -> str:
        if not value or Path(value).name != value or any(char.isspace() for char in value):
            raise ValueError("sandbox binary must be a bare executable name")
        return value

    @field_validator("image")
    @classmethod
    def validate_image(cls, value: str) -> str:
        if not value or value != value.strip() or any(char.isspace() for char in value):
            raise ValueError("Docker image must be a non-empty image reference")
        leaf = value.rsplit("/", 1)[-1]
        if value.endswith(":latest") or (":" not in leaf and "@" not in value):
            raise ValueError("Docker image must use an explicit non-latest tag or digest")
        return value

    @property
    def is_digest_pinned(self) -> bool:
        return bool(_DIGEST_IMAGE.fullmatch(self.image))


class LocalDevelopmentSandboxConfig(_FrozenConfig):
    backend: Literal["local"] = "local"
    required: Literal[False] = False
    acknowledge_unsafe: Literal[True]
    limits: SandboxLimitsConfig = Field(default_factory=SandboxLimitsConfig)
    network: SandboxNetworkConfig = Field(default_factory=SandboxNetworkConfig)


SandboxConfig = Annotated[
    DockerSandboxConfig | LocalDevelopmentSandboxConfig,
    Field(discriminator="backend"),
]


class WebChannelConfig(_FrozenConfig):
    enabled: bool = True
    websocket_path: str = "/ws"
    session_path: str = "/auth/session"
    session_lifetime_seconds: int = Field(default=300, ge=30, le=900)
    authentication_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    max_frame_bytes: Literal[1_048_576] = 1_048_576
    allowed_origins: tuple[str, ...] = ()
    approval_mode: Literal["interactive"] = "interactive"
    credentials_ref: str = "web/default"

    @field_validator("authentication_timeout_seconds", mode="before")
    @classmethod
    def validate_authentication_timeout_type(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("Web authentication timeout must be a number")
        return value

    @field_validator("websocket_path")
    @classmethod
    def validate_websocket_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("websocket_path must be an absolute URL path")
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("websocket_path must not contain an origin, query, or fragment")
        return value

    @field_validator("session_path")
    @classmethod
    def validate_session_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("session_path must be an absolute URL path")
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("session_path must not contain an origin, query, or fragment")
        return value

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for origin in value:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"invalid Web origin: {origin!r}")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("Web origins must not contain user information")
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError("Web origins must contain only scheme and authority")
        if len(value) != len(set(value)):
            raise ValueError("Web allowed_origins must not contain duplicates")
        return value

    @field_validator("credentials_ref")
    @classmethod
    def validate_credentials_ref(cls, value: str) -> str:
        return _secret_ref(value, "Web credentials_ref")


class FeishuChannelConfig(_FrozenConfig):
    enabled: bool = False
    transport: Literal["long_connection", "webhook"] = "long_connection"
    app_id: str | None = None
    credentials_ref: str = "feishu/default"
    webhook_path: str = "/channels/feishu/events"
    default_access: Literal["read_only"] = "read_only"
    interactive_approval: Literal[False] = False
    allow_delete: Literal[False] = False
    authentication_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    writable_roots: tuple[WorkspaceRootConfig, ...] = ()

    @field_validator("authentication_timeout_seconds", mode="before")
    @classmethod
    def validate_authentication_timeout_type(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("Feishu authentication timeout must be a number")
        return value

    @field_validator("credentials_ref")
    @classmethod
    def validate_credentials_ref(cls, value: str) -> str:
        return _secret_ref(value, "Feishu credentials_ref")

    @field_validator("webhook_path")
    @classmethod
    def validate_webhook_path(cls, value: str) -> str:
        parsed = urlsplit(value)
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("Feishu webhook_path must be an absolute URL path")
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Feishu webhook_path must not contain an origin, query, or fragment")
        return value

    @model_validator(mode="after")
    def validate_enabled_channel(self) -> FeishuChannelConfig:
        if self.enabled and not self.app_id:
            raise ValueError("enabled Feishu channel requires app_id")
        if self.app_id is not None and not self.app_id.strip():
            raise ValueError("Feishu app_id must not be blank")
        if len(self.writable_roots) != len(set(self.writable_roots)):
            raise ValueError("Feishu writable_roots must not contain duplicates")
        return self


class ChannelConversationRouteConfig(_FrozenConfig):
    """Bind one company conversation to a product in a multi-product bundle."""

    channel: Literal["web", "feishu"]
    conversation_id: str
    product_id: str

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, value: str) -> str:
        if not value or len(value) > 1024 or "\x00" in value:
            raise ValueError("channel conversation_id must be bounded and NUL-free")
        return value

    @field_validator("product_id")
    @classmethod
    def validate_product_id(cls, value: str) -> str:
        return _identifier(value, "channel route product_id")


class ChannelsConfig(_FrozenConfig):
    web: WebChannelConfig = Field(default_factory=WebChannelConfig)
    feishu: FeishuChannelConfig = Field(default_factory=FeishuChannelConfig)
    routes: tuple[ChannelConversationRouteConfig, ...] = ()
    agent_ids: dict[str, str] = Field(default_factory=dict)
    share_conversation_sessions: bool = False
    authorization_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)

    @field_validator("authorization_timeout_seconds", mode="before")
    @classmethod
    def validate_authorization_timeout_type(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("channel authorization timeout must be a number")
        return value

    @field_validator("agent_ids")
    @classmethod
    def validate_agent_ids(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            _identifier(product, "agent product_id"): _identifier(agent, "agent_id")
            for product, agent in value.items()
        }

    @model_validator(mode="after")
    def validate_channel_enabled(self) -> ChannelsConfig:
        if not self.web.enabled and not self.feishu.enabled:
            raise ValueError("at least one company channel (Web or Feishu) must be enabled")
        keys = [(route.channel, route.conversation_id) for route in self.routes]
        if len(keys) != len(set(keys)):
            raise ValueError("channel conversation routes must not contain duplicates")
        if any(route.channel == "web" and not self.web.enabled for route in self.routes):
            raise ValueError("Web conversation routes require the Web channel")
        if any(route.channel == "feishu" and not self.feishu.enabled for route in self.routes):
            raise ValueError("Feishu conversation routes require the Feishu channel")
        reserved = {"/health/live", "/health/ready"}
        active_paths: list[str] = []
        if self.web.enabled:
            active_paths.extend((self.web.websocket_path, self.web.session_path))
        if self.feishu.enabled and self.feishu.transport == "webhook":
            active_paths.append(self.feishu.webhook_path)
        if len(active_paths) != len(set(active_paths)):
            raise ValueError("enabled company channel paths must be distinct")
        if reserved.intersection(active_paths):
            raise ValueError("company channel paths must not replace health endpoints")
        return self


class ModelProfileConfig(_FrozenConfig):
    """One server-owned model endpoint; credentials remain in SuiteHarnessSecrets."""

    profile_id: str
    provider_id: str
    model: str
    credentials_ref: str | None = None
    base_url: str | None = None
    timeout_seconds: float = Field(default=120.0, gt=0.0, le=3600.0)
    max_retries: int = Field(default=2, ge=0, le=10)
    allowed_models: frozenset[str] = frozenset()
    default_headers: dict[str, str] = Field(default_factory=dict)
    provider_options: dict[str, JsonValue] = Field(default_factory=dict)
    allow_plain_http: bool = False

    @field_validator("profile_id", "provider_id")
    @classmethod
    def validate_ids(cls, value: str, info: object) -> str:
        return _identifier(value, getattr(info, "field_name", "model identifier"))

    @field_validator("credentials_ref")
    @classmethod
    def validate_credentials_ref(cls, value: str | None) -> str | None:
        return None if value is None else _secret_ref(value, "model credentials_ref")

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip() or len(value) > 512 or "\x00" in value:
            raise ValueError("model must be a bounded non-blank string")
        return value

    @field_validator("default_headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name, content in value.items():
            if not name or any(char in name for char in "\r\n:"):
                raise ValueError("invalid model header name")
            if "\r" in content or "\n" in content:
                raise ValueError("invalid model header value")
            if name.lower() in {"authorization", "proxy-authorization", "x-api-key"}:
                raise ValueError("model credentials must use credentials_ref, not default_headers")
        return value

    @model_validator(mode="after")
    def validate_endpoint(self) -> ModelProfileConfig:
        if self.base_url is None:
            return self
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("model base_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("model base_url must not contain credentials")
        if parsed.scheme == "http" and not self.allow_plain_http:
            raise ValueError("plain HTTP model endpoints require allow_plain_http=true")
        return self

    def to_runtime(self):  # type: ignore[no-untyped-def]
        """Create the provider-neutral runtime profile without carrying secrets."""

        from suiteharness.models import ModelProfile

        return ModelProfile(
            profile_id=self.profile_id,
            provider_id=self.provider_id,
            model=self.model,
            credential_ref=self.credentials_ref,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            allowed_models=self.allowed_models,
            default_headers=self.default_headers,
            provider_options=self.provider_options,
            allow_plain_http=self.allow_plain_http,
        )


class ModelRouteConfig(_FrozenConfig):
    route_id: str
    primary_profile: str
    fallback_profiles: tuple[str, ...] = ()

    @field_validator("route_id", "primary_profile", "fallback_profiles")
    @classmethod
    def validate_profile_ids(cls, value: str | tuple[str, ...], info: object):  # type: ignore[no-untyped-def]
        if isinstance(value, tuple):
            return tuple(_identifier(item, "fallback profile") for item in value)
        return _identifier(value, getattr(info, "field_name", "model route"))

    @model_validator(mode="after")
    def validate_unique_profiles(self) -> ModelRouteConfig:
        profiles = (self.primary_profile, *self.fallback_profiles)
        if len(profiles) != len(set(profiles)):
            raise ValueError("a model route must not repeat profiles")
        return self

    def to_runtime(self):  # type: ignore[no-untyped-def]
        from suiteharness.models import ModelRoute

        return ModelRoute(
            route_id=self.route_id,
            primary_profile=self.primary_profile,
            fallback_profiles=self.fallback_profiles,
        )


class ModelsConfig(_FrozenConfig):
    profiles: tuple[ModelProfileConfig, ...]
    routes: tuple[ModelRouteConfig, ...]
    default_route: str
    product_routes: dict[str, str] = Field(default_factory=dict)

    @field_validator("default_route")
    @classmethod
    def validate_default_route(cls, value: str) -> str:
        return _identifier(value, "default model route")

    @field_validator("product_routes")
    @classmethod
    def validate_product_routes(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            _identifier(product, "product route product_id"): _identifier(route, "model route")
            for product, route in value.items()
        }

    @model_validator(mode="after")
    def validate_graph(self) -> ModelsConfig:
        profile_ids = [item.profile_id for item in self.profiles]
        route_ids = [item.route_id for item in self.routes]
        if not profile_ids or len(profile_ids) != len(set(profile_ids)):
            raise ValueError("model profiles must be non-empty and unique")
        if not route_ids or len(route_ids) != len(set(route_ids)):
            raise ValueError("model routes must be non-empty and unique")
        known_profiles = set(profile_ids)
        for route in self.routes:
            missing = {route.primary_profile, *route.fallback_profiles} - known_profiles
            if missing:
                raise ValueError(f"model route references unknown profiles: {sorted(missing)}")
        known_routes = set(route_ids)
        if self.default_route not in known_routes:
            raise ValueError("default model route does not exist")
        if missing_routes := set(self.product_routes.values()) - known_routes:
            raise ValueError(f"product model routing references unknown routes: {sorted(missing_routes)}")
        return self


class BaiduQianfanSearchConfig(_FrozenConfig):
    kind: Literal["baidu_qianfan"] = "baidu_qianfan"
    provider_id: Literal["baidu-qianfan"] = "baidu-qianfan"
    credentials_ref: str = "search/baidu-qianfan"
    endpoint: str = "https://qianfan.baidubce.com/v2/ai_search/web_search"
    egress_profile: str | None = None

    @field_validator("egress_profile")
    @classmethod
    def validate_egress_profile(cls, value: str | None) -> str | None:
        return None if value is None else _identifier(value, "search egress_profile")

    @field_validator("credentials_ref")
    @classmethod
    def validate_credentials_ref(cls, value: str) -> str:
        return _secret_ref(value, "search credentials_ref")

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Baidu Qianfan search endpoint must be an absolute HTTPS URL")
        return value


class FoundrySearchConfig(_FrozenConfig):
    kind: Literal["microsoft_foundry"] = "microsoft_foundry"
    provider_id: Literal["microsoft-foundry-bing-grounding"] = (
        "microsoft-foundry-bing-grounding"
    )
    credentials_ref: str
    project_endpoint: str
    connection_id: str

    @field_validator("connection_id")
    @classmethod
    def validate_connection_id(cls, value: str) -> str:
        if not value.strip() or len(value) > 2048 or "\x00" in value:
            raise ValueError("Foundry connection_id must be a bounded non-blank identifier")
        return value

    @field_validator("credentials_ref")
    @classmethod
    def validate_credentials_ref(cls, value: str) -> str:
        return _secret_ref(value, "Foundry credentials_ref")

    @field_validator("project_endpoint")
    @classmethod
    def validate_project_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Foundry project_endpoint must be an absolute HTTPS URL")
        return value


SearchProviderConfig = Annotated[
    BaiduQianfanSearchConfig | FoundrySearchConfig,
    Field(discriminator="kind"),
]


class WebSearchConfig(_FrozenConfig):
    default_provider: str
    providers: tuple[SearchProviderConfig, ...]

    @model_validator(mode="after")
    def validate_providers(self) -> WebSearchConfig:
        ids = [item.provider_id for item in self.providers]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("search providers must be non-empty and unique")
        if self.default_provider not in ids:
            raise ValueError("default search provider is not configured")
        return self


class DirectFetchConfig(_FrozenConfig):
    kind: Literal["direct"] = "direct"
    name: str = "direct"


class ManagedProxyFetchConfig(_FrozenConfig):
    kind: Literal["managed_proxy"] = "managed_proxy"
    name: str
    endpoint: str
    credentials_ref: str | None = None

    @field_validator("credentials_ref")
    @classmethod
    def validate_credentials_ref(cls, value: str | None) -> str | None:
        return None if value is None else _secret_ref(value, "proxy credentials_ref")

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("managed proxy endpoint must be an absolute HTTP URL without userinfo")
        return value


class BrowserWorkerFetchConfig(_FrozenConfig):
    kind: Literal["browser_worker"] = "browser_worker"
    name: str
    endpoint: str
    credentials_ref: str

    @field_validator("credentials_ref")
    @classmethod
    def validate_credentials_ref(cls, value: str) -> str:
        return _secret_ref(value, "browser worker credentials_ref")

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("browser worker endpoint must be an absolute HTTPS URL")
        return value


FetchRouteConfig = Annotated[
    DirectFetchConfig | ManagedProxyFetchConfig | BrowserWorkerFetchConfig,
    Field(discriminator="kind"),
]


class WebFetchConfig(_FrozenConfig):
    default_route: str
    routes: tuple[FetchRouteConfig, ...]

    @model_validator(mode="after")
    def validate_routes(self) -> WebFetchConfig:
        names = [route.name for route in self.routes]
        if not names or len(names) != len(set(names)):
            raise ValueError("web fetch routes must be non-empty and unique")
        for name in names:
            _identifier(name, "web fetch route")
        if self.default_route not in names:
            raise ValueError("default web fetch route is not configured")
        return self


class WebToolsConfig(_FrozenConfig):
    search: WebSearchConfig
    fetch: WebFetchConfig
    search_providers_by_product: dict[str, frozenset[str]] = Field(default_factory=dict)
    fetch_routes_by_product: dict[str, frozenset[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_product_access(self) -> WebToolsConfig:
        configured_search = {provider.provider_id for provider in self.search.providers}
        configured_fetch = {route.name for route in self.fetch.routes}
        for product_id, providers in self.search_providers_by_product.items():
            _identifier(product_id, "Web search product_id")
            unknown = set(providers) - configured_search
            if unknown:
                raise ValueError(
                    f"product Web search access references unknown providers: {sorted(unknown)!r}"
                )
        for product_id, routes in self.fetch_routes_by_product.items():
            _identifier(product_id, "Web fetch product_id")
            unknown = set(routes) - configured_fetch
            if unknown:
                raise ValueError(
                    f"product Web fetch access references unknown routes: {sorted(unknown)!r}"
                )
        return self


class McpProductChannelAccessConfig(_FrozenConfig):
    """Exact MCP allowlist for one channel/product route.

    ``allow_servers`` opts into every tool currently discovered from an exact
    server. ``allow_tools`` opts into exact remote tool names under a server.
    The empty default grants no MCP authority.
    """

    allow_servers: frozenset[str] = frozenset()
    allow_tools: dict[str, frozenset[str]] = Field(default_factory=dict)

    @field_validator("allow_servers")
    @classmethod
    def validate_allowed_servers(cls, value: frozenset[str]) -> frozenset[str]:
        return frozenset(_identifier(item, "MCP allowlist server_id") for item in value)

    @field_validator("allow_tools")
    @classmethod
    def validate_allowed_tools(
        cls, value: dict[str, frozenset[str]]
    ) -> dict[str, frozenset[str]]:
        result: dict[str, frozenset[str]] = {}
        for server_id, names in value.items():
            server_id = _identifier(server_id, "MCP tool allowlist server_id")
            if not names:
                raise ValueError("MCP allow_tools entries must not be empty")
            for name in names:
                if not name.strip() or len(name) > 256 or "\x00" in name:
                    raise ValueError(
                        "MCP allowlist remote tool names must be bounded and non-blank"
                    )
            result[server_id] = frozenset(names)
        return result

    @model_validator(mode="after")
    def reject_redundant_server_rules(self) -> McpProductChannelAccessConfig:
        overlap = set(self.allow_servers) & set(self.allow_tools)
        if overlap:
            raise ValueError(
                "MCP server and tool allowlists must not overlap: "
                f"{sorted(overlap)!r}"
            )
        return self


class McpChannelAccessConfig(_FrozenConfig):
    """MCP authority keyed first by company channel, then product."""

    web: dict[str, McpProductChannelAccessConfig] = Field(default_factory=dict)
    feishu: dict[str, McpProductChannelAccessConfig] = Field(default_factory=dict)

    @field_validator("web", "feishu")
    @classmethod
    def validate_product_ids(
        cls, value: dict[str, McpProductChannelAccessConfig]
    ) -> dict[str, McpProductChannelAccessConfig]:
        return {
            _identifier(product_id, "MCP channel access product_id"): rule
            for product_id, rule in value.items()
        }


class McpConfig(_FrozenConfig):
    """MCP servers selected explicitly for each product bundle."""

    servers_by_product: dict[str, tuple[McpServerConfig, ...]] = Field(default_factory=dict)
    tool_security_overrides: dict[
        str, dict[str, dict[str, McpToolSecurityOverride]]
    ] = Field(default_factory=dict)
    channel_access: McpChannelAccessConfig = Field(default_factory=McpChannelAccessConfig)

    @field_validator("servers_by_product")
    @classmethod
    def validate_product_servers(
        cls, value: dict[str, tuple[McpServerConfig, ...]]
    ) -> dict[str, tuple[McpServerConfig, ...]]:
        for product_id, servers in value.items():
            _identifier(product_id, "MCP product_id")
            for server in servers:
                if server.credential_ref is not None:
                    _secret_ref(server.credential_ref, "MCP credential_ref")
            server_ids = [server.server_id for server in servers]
            if len(server_ids) != len(set(server_ids)):
                raise ValueError(
                    f"MCP server ids must be unique within product {product_id!r}"
                )
        return value

    @model_validator(mode="after")
    def validate_tool_overrides(self) -> McpConfig:
        unknown_products = set(self.tool_security_overrides) - set(self.servers_by_product)
        if unknown_products:
            raise ValueError(
                "MCP tool overrides reference products without MCP servers: "
                f"{sorted(unknown_products)}"
            )
        for product_id, server_overrides in self.tool_security_overrides.items():
            configured_servers = {
                server.server_id for server in self.servers_by_product[product_id]
            }
            unknown_servers = set(server_overrides) - configured_servers
            if unknown_servers:
                raise ValueError(
                    f"MCP tool overrides for {product_id!r} reference unknown servers: "
                    f"{sorted(unknown_servers)}"
                )
            for server_id, overrides in server_overrides.items():
                _identifier(server_id, "MCP override server_id")
                for remote_name in overrides:
                    if (
                        not remote_name.strip()
                        or len(remote_name) > 256
                        or "\x00" in remote_name
                    ):
                        raise ValueError("MCP override remote tool names must be bounded")
        enabled_servers = {
            product_id: {server.server_id for server in servers if server.enabled}
            for product_id, servers in self.servers_by_product.items()
        }
        for channel, product_rules in (
            ("web", self.channel_access.web),
            ("feishu", self.channel_access.feishu),
        ):
            unknown_products = set(product_rules) - set(enabled_servers)
            if unknown_products:
                raise ValueError(
                    f"MCP {channel} access references products without MCP servers: "
                    f"{sorted(unknown_products)!r}"
                )
            for product_id, rule in product_rules.items():
                referenced = set(rule.allow_servers) | set(rule.allow_tools)
                unknown_servers = referenced - enabled_servers[product_id]
                if unknown_servers:
                    raise ValueError(
                        f"MCP {channel} access for {product_id!r} references unknown "
                        f"or disabled servers: {sorted(unknown_servers)!r}"
                    )
        return self


class PluginsConfig(_FrozenConfig):
    """Closed plugin inventory for one source-deployed server installation."""

    allowed_roots: tuple[Path, ...] = ()
    sources: tuple[PluginSourceDeclaration, ...] = ()
    trusted_in_process_plugins: frozenset[str] = frozenset()
    require_signatures: bool = True

    @field_validator("allowed_roots")
    @classmethod
    def validate_allowed_roots(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        normalized: list[Path] = []
        for root in value:
            absolute = _absolute_path(root, "plugin allowed_root")
            normalized.append(absolute.resolve(strict=False))
        if len(normalized) != len(set(normalized)):
            raise ValueError("plugin allowed_roots must not contain duplicates")
        return tuple(normalized)

    @field_validator("trusted_in_process_plugins")
    @classmethod
    def validate_trusted_plugins(cls, value: frozenset[str]) -> frozenset[str]:
        return frozenset(_identifier(item, "trusted plugin_id") for item in value)

    @model_validator(mode="after")
    def validate_sources(self) -> PluginsConfig:
        if self.sources and not self.allowed_roots:
            raise ValueError("plugin sources require at least one allowed_root")
        seen_paths: set[Path] = set()
        for source in self.sources:
            candidate = source.path.resolve(strict=False)
            if candidate in seen_paths:
                raise ValueError("plugin source paths must not contain duplicates")
            seen_paths.add(candidate)
            if not any(candidate.is_relative_to(root) for root in self.allowed_roots):
                raise ValueError(f"plugin source is outside configured allowed_roots: {source.path}")
        return self


class SuiteHarnessConfig(_FrozenConfig):
    """Non-secret deployment configuration."""

    config_version: Literal[1] = 1
    deployment: DeploymentConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    customer_bundle: CustomerBundleManifest
    workspace: WorkspaceConfig
    storage: StorageConfig
    sandbox: SandboxConfig
    models: ModelsConfig
    web_tools: WebToolsConfig
    mcp: McpConfig = Field(default_factory=McpConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)

    @model_validator(mode="after")
    def validate_environment(self) -> SuiteHarnessConfig:
        selected_products = {
            product.product_id for product in self.customer_bundle.products
        }
        routed_products = set(self.models.product_routes)
        if unknown_routes := routed_products - selected_products:
            raise ValueError(
                "model product_routes reference products outside customer_bundle: "
                f"{sorted(unknown_routes)}"
            )
        mcp_products = set(self.mcp.servers_by_product)
        if unknown_mcp := mcp_products - selected_products:
            raise ValueError(
                "MCP configuration references products outside customer_bundle: "
                f"{sorted(unknown_mcp)}"
            )
        egress_products = (
            set(self.sandbox.network.allowed_profiles_by_product)
            | set(self.web_tools.search_providers_by_product)
            | set(self.web_tools.fetch_routes_by_product)
        )
        if unknown_egress := egress_products - selected_products:
            raise ValueError(
                "egress access references products outside customer_bundle: "
                f"{sorted(unknown_egress)}"
            )
        if unknown_shared := set(self.workspace.shared_access_by_product) - selected_products:
            raise ValueError(
                "shared workspace access references products outside customer_bundle: "
                f"{sorted(unknown_shared)}"
            )
        channel_products = {
            route.product_id for route in self.channels.routes
        } | set(self.channels.agent_ids)
        if unknown_channel_products := channel_products - selected_products:
            raise ValueError(
                "channel configuration references products outside customer_bundle: "
                f"{sorted(unknown_channel_products)}"
            )
        if self.mcp.channel_access.web and not self.channels.web.enabled:
            raise ValueError("MCP Web access requires the Web channel to be enabled")
        if self.mcp.channel_access.feishu and not self.channels.feishu.enabled:
            raise ValueError("MCP Feishu access requires the Feishu channel to be enabled")
        if (
            self.channels.feishu.enabled
            and len(selected_products) > 1
            and not any(route.channel == "feishu" for route in self.channels.routes)
        ):
            raise ValueError(
                "multi-product Feishu deployments require a conversation route"
            )
        fetch_routes = {route.name: route for route in self.web_tools.fetch.routes}
        for provider in self.web_tools.search.providers:
            if not isinstance(provider, BaiduQianfanSearchConfig):
                continue
            route_name = provider.egress_profile or self.web_tools.fetch.default_route
            route = fetch_routes[route_name] if route_name in fetch_routes else None
            if route is None:
                raise ValueError(
                    f"search egress_profile references an unknown fetch route: {route_name!r}"
                )
            if isinstance(route, BrowserWorkerFetchConfig):
                raise ValueError("structured search cannot use a GET-only browser_worker route")
        if not self.workspace.shared_enabled and any(
            root.space == "shared" for root in self.channels.feishu.writable_roots
        ):
            raise ValueError("Feishu shared writable roots require workspace.shared_enabled")
        if any(root.space == "shared" for root in self.channels.feishu.writable_roots) and not any(
            access == "read_write"
            for access in self.workspace.shared_access_by_product.values()
        ):
            raise ValueError(
                "Feishu shared writable roots require explicit read_write product access"
            )
        if self.deployment.environment == "production":
            if not isinstance(self.sandbox, DockerSandboxConfig):
                raise ValueError("production deployments require the Docker sandbox backend")
            if not self.plugins.require_signatures:
                raise ValueError("production deployments require plugin signatures")
            if not self.sandbox.is_digest_pinned:
                raise ValueError("production sandbox images must be pinned by sha256 digest")
            insecure_credential_profiles = [
                profile.profile_id
                for profile in self.models.profiles
                if profile.credentials_ref is not None
                and profile.base_url is not None
                and urlsplit(profile.base_url).scheme == "http"
            ]
            if insecure_credential_profiles:
                raise ValueError(
                    "production model endpoints carrying credentials must use HTTPS: "
                    f"{sorted(insecure_credential_profiles)!r}"
                )
            if self.channels.web.enabled:
                if not self.channels.web.allowed_origins:
                    raise ValueError("production Web channel requires allowed_origins")
                if any(not origin.startswith("https://") for origin in self.channels.web.allowed_origins):
                    raise ValueError("production Web origins must use HTTPS")
        return self


class FeishuCredentials(_FrozenConfig):
    app_secret: SecretStr = Field(min_length=1)
    verification_token: SecretStr | None = None
    encrypt_key: SecretStr | None = None


class WebCredentials(_FrozenConfig):
    session_signing_key: SecretStr = Field(min_length=32)


class ProviderCredentials(_FrozenConfig):
    api_key: SecretStr | None = None
    access_key_id: SecretStr | None = None
    secret_access_key: SecretStr | None = None
    session_token: SecretStr | None = None
    service_account_json: SecretStr | None = None
    endpoint_credential: SecretStr | None = None

    @model_validator(mode="after")
    def require_credential(self) -> ProviderCredentials:
        if all(
            value is None
            for value in (
                self.api_key,
                self.access_key_id,
                self.secret_access_key,
                self.session_token,
                self.service_account_json,
                self.endpoint_credential,
            )
        ):
            raise ValueError("a provider credential entry must contain at least one secret")
        if (self.access_key_id is None) != (self.secret_access_key is None):
            raise ValueError("AWS access_key_id and secret_access_key must be configured together")
        return self


class ServiceCredentials(_FrozenConfig):
    token: SecretStr | None = None
    api_key: SecretStr | None = None
    client_id: SecretStr | None = None
    client_secret: SecretStr | None = None
    headers: dict[str, SecretStr] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_credential(self) -> ServiceCredentials:
        if all(
            value is None or value == {}
            for value in (self.token, self.api_key, self.client_id, self.client_secret, self.headers)
        ):
            raise ValueError("a service credential entry must contain at least one secret")
        return self


class SuiteHarnessSecrets(_FrozenConfig):
    """Secrets loaded only from the separate secret configuration file.

    ``SecretStr`` keeps representations and JSON serialization redacted.  Code
    that needs a raw value must explicitly call ``get_secret_value()`` at the
    final integration boundary.
    """

    config_version: Literal[1] = 1
    web: dict[str, WebCredentials] = Field(default_factory=dict)
    feishu: dict[str, FeishuCredentials] = Field(default_factory=dict)
    model_providers: dict[str, ProviderCredentials] = Field(default_factory=dict)
    services: dict[str, ServiceCredentials] = Field(default_factory=dict)

    @field_validator("web", "feishu", "model_providers", "services")
    @classmethod
    def validate_secret_refs(cls, value: dict[str, object]) -> dict[str, object]:
        for key in value:
            _secret_ref(key, "secret reference")
        return value


def validate_secret_references(config: SuiteHarnessConfig, secrets: SuiteHarnessSecrets) -> None:
    """Validate cross-file credential references without exposing secret values."""

    if config.channels.web.enabled:
        ref = config.channels.web.credentials_ref
        if ref not in secrets.web:
            raise ValueError(f"Web credentials_ref {ref!r} was not found in the secrets file")
    if config.channels.feishu.enabled:
        ref = config.channels.feishu.credentials_ref
        credentials = secrets.feishu.get(ref)
        if credentials is None:
            raise ValueError(f"Feishu credentials_ref {ref!r} was not found in the secrets file")
        if (
            config.channels.feishu.transport == "webhook"
            and credentials.verification_token is None
        ):
            raise ValueError("Feishu webhook transport requires a verification_token")
        if config.channels.feishu.transport == "webhook" and credentials.encrypt_key is None:
            raise ValueError(
                "Feishu webhook transport requires an encrypt_key for raw-body signatures"
            )
    for profile in config.models.profiles:
        if profile.credentials_ref is not None and profile.credentials_ref not in secrets.model_providers:
            raise ValueError(
                f"model credentials_ref {profile.credentials_ref!r} was not found in the secrets file"
            )
    service_refs: set[str] = set()
    for provider in config.web_tools.search.providers:
        service_refs.add(provider.credentials_ref)
    for route in config.web_tools.fetch.routes:
        ref = getattr(route, "credentials_ref", None)
        if ref is not None:
            service_refs.add(ref)
    for servers in config.mcp.servers_by_product.values():
        for server in servers:
            if server.credential_ref is not None:
                service_refs.add(server.credential_ref)
    missing_services = service_refs - set(secrets.services)
    if missing_services:
        raise ValueError(
            f"service credentials_ref entries were not found: {sorted(missing_services)}"
        )


__all__ = [
    "ChannelsConfig",
    "ChannelConversationRouteConfig",
    "DeploymentConfig",
    "DockerSandboxConfig",
    "FeishuChannelConfig",
    "FeishuCredentials",
    "SuiteHarnessConfig",
    "SuiteHarnessSecrets",
    "LocalDevelopmentSandboxConfig",
    "McpChannelAccessConfig",
    "McpConfig",
    "McpProductChannelAccessConfig",
    "PluginsConfig",
    "ProviderCredentials",
    "ServiceCredentials",
    "ModelProfileConfig",
    "ModelRouteConfig",
    "ModelsConfig",
    "BaiduQianfanSearchConfig",
    "FoundrySearchConfig",
    "WebSearchConfig",
    "DirectFetchConfig",
    "ManagedProxyFetchConfig",
    "BrowserWorkerFetchConfig",
    "WebFetchConfig",
    "WebToolsConfig",
    "SandboxConfig",
    "SandboxLimitsConfig",
    "SandboxNetworkConfig",
    "StorageConfig",
    "WebChannelConfig",
    "WebCredentials",
    "WorkspaceConfig",
    "WorkspaceRootConfig",
    "validate_secret_references",
]

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from suiteharness.config import (
    ConfigLoadError,
    SuiteHarnessConfig,
    SuiteHarnessConfigLoader,
)


def _public_config(workspace: Path) -> dict[str, object]:
    return {
        "config_version": 1,
        "deployment": {
            "mode": "server",
            "environment": "development",
            "instance_id": "acme-main",
            "tenant_id": "acme",
        },
        "customer_bundle": {
            "customer_bundle_id": "acme-products",
            "version": "1.0.0",
            "harness_api": ">=0.1,<0.2",
            "products": [
                {"product_id": "sales", "version": "==1.0.0", "config": {}}
            ],
        },
        "workspace": {"root": str(workspace), "shared_enabled": True},
        "storage": {"root": str(workspace.parent / "state")},
        "sandbox": {
            "backend": "docker",
            "required": True,
            "image": "registry.example/suiteharness-sandbox:0.1.0",
        },
        "models": {
            "profiles": [
                {
                    "profile_id": "local-qwen",
                    "provider_id": "ollama",
                    "model": "qwen3",
                    "base_url": "http://ollama:11434/v1",
                    "allow_plain_http": True,
                }
            ],
            "routes": [{"route_id": "default", "primary_profile": "local-qwen"}],
            "default_route": "default",
        },
        "web_tools": {
            "search": {
                "default_provider": "baidu-qianfan",
                "providers": [
                    {
                        "kind": "baidu_qianfan",
                        "credentials_ref": "search/baidu-qianfan",
                    }
                ],
            },
            "fetch": {
                "default_route": "direct",
                "routes": [{"kind": "direct", "name": "direct"}],
            },
        },
        "channels": {
            "web": {
                "enabled": True,
                "websocket_path": "/ws",
                "credentials_ref": "web/default",
            },
            "feishu": {"enabled": False},
        },
    }


def _secret_config() -> dict[str, object]:
    return {
        "config_version": 1,
        "web": {
            "web/default": {
                "session_signing_key": "a-very-long-server-owned-signing-key",
            }
        },
        "services": {"search/baidu-qianfan": {"token": "server-search-token"}},
    }


def _write_json_yaml(path: Path, value: object, *, private: bool = False) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    if private and os.name != "nt":
        path.chmod(0o600)


def test_loader_keeps_public_and_secret_files_separate_and_redacted(tmp_path: Path) -> None:
    public = tmp_path / "suiteharness.yaml"
    private = tmp_path / "suiteharness.secrets.yaml"
    _write_json_yaml(public, _public_config(tmp_path / "workspaces"))
    _write_json_yaml(private, _secret_config(), private=True)

    loaded = SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)

    assert loaded.config.deployment.mode == "server"
    assert loaded.config.deployment.tenant_id == "acme"
    assert loaded.config.channels.web.websocket_path == "/ws"
    assert "a-very-long-server-owned-signing-key" not in repr(loaded)
    assert "a-very-long-server-owned-signing-key" not in loaded.secrets.model_dump_json()


def test_loader_defers_missing_search_secret_until_runtime_grants_are_known(
    tmp_path: Path,
) -> None:
    public = tmp_path / "suiteharness.yaml"
    private = tmp_path / "suiteharness.secrets.yaml"
    secrets = _secret_config()
    secrets["services"] = {}
    _write_json_yaml(public, _public_config(tmp_path / "workspaces"))
    _write_json_yaml(private, secrets, private=True)

    loaded = SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)

    assert loaded.secrets.services == {}
    assert loaded.config.web_tools.search.providers[0].provider_id == "baidu-qianfan"


def test_loader_rejects_hardlinked_public_and_secret_documents(tmp_path: Path) -> None:
    public = tmp_path / "suiteharness.yaml"
    private = tmp_path / "suiteharness.secrets.yaml"
    _write_json_yaml(public, _public_config(tmp_path / "workspaces"), private=True)
    os.link(public, private)

    with pytest.raises(ConfigLoadError, match="separate files"):
        SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)


def test_loader_rejects_symbolic_link_document(tmp_path: Path) -> None:
    target = tmp_path / "actual.yaml"
    public = tmp_path / "suiteharness.yaml"
    private = tmp_path / "suiteharness.secrets.yaml"
    _write_json_yaml(target, _public_config(tmp_path / "workspaces"))
    try:
        public.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable to this test process")
    _write_json_yaml(private, _secret_config(), private=True)

    with pytest.raises(ConfigLoadError, match="symbolic link"):
        SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)


def test_production_loader_rejects_example_placeholders(tmp_path: Path) -> None:
    public = tmp_path / "suiteharness.yaml"
    private = tmp_path / "suiteharness.secrets.yaml"
    raw = _public_config(tmp_path / "workspaces")
    raw["deployment"]["environment"] = "production"  # type: ignore[index]
    raw["sandbox"]["image"] = "registry.acme.cn/sandbox@sha256:" + "a" * 64  # type: ignore[index]
    raw["channels"]["web"]["allowed_origins"] = ["https://assistant.acme.cn"]  # type: ignore[index]
    profile = raw["models"]["profiles"][0]  # type: ignore[index]
    profile["model"] = "replace-with-approved-model"  # type: ignore[index]
    _write_json_yaml(public, raw)
    _write_json_yaml(private, _secret_config(), private=True)

    with pytest.raises(ConfigLoadError, match="placeholder"):
        SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)

    profile["model"] = "qwen3"  # type: ignore[index]
    raw["sandbox"]["image"] = "registry.acme.cn/sandbox@sha256:" + "0" * 64  # type: ignore[index]
    _write_json_yaml(public, raw)
    with pytest.raises(ConfigLoadError, match="all-zero"):
        SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)

    raw["sandbox"]["image"] = "registry.example.com/sandbox@sha256:" + "a" * 64  # type: ignore[index]
    _write_json_yaml(public, raw)
    with pytest.raises(ConfigLoadError, match="reserved example hostname"):
        SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)

    raw["sandbox"]["image"] = "registry.acme.cn/sandbox@sha256:" + "a" * 64  # type: ignore[index]
    profile["base_url"] = None  # type: ignore[index]
    _write_json_yaml(public, raw)
    loaded = SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)
    assert loaded.config.models.profiles[0].base_url is None


def test_production_loader_rejects_placeholder_and_dummy_secrets(tmp_path: Path) -> None:
    public = tmp_path / "suiteharness.yaml"
    private = tmp_path / "suiteharness.secrets.yaml"
    raw = _public_config(tmp_path / "workspaces")
    raw["deployment"]["environment"] = "production"  # type: ignore[index]
    raw["sandbox"]["image"] = "registry.acme.cn/sandbox@sha256:" + "a" * 64  # type: ignore[index]
    raw["channels"]["web"]["allowed_origins"] = ["https://assistant.acme.cn"]  # type: ignore[index]
    _write_json_yaml(public, raw)

    secrets = _secret_config()
    secrets["services"]["search/baidu-qianfan"]["token"] = "replace-with-token"  # type: ignore[index]
    _write_json_yaml(private, secrets, private=True)
    with pytest.raises(ConfigLoadError, match="placeholder"):
        SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)

    secrets["services"]["search/baidu-qianfan"]["token"] = "A" * 32  # type: ignore[index]
    _write_json_yaml(private, secrets, private=True)
    with pytest.raises(ConfigLoadError, match="dummy"):
        SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)


def test_model_capabilities_flow_from_config_to_runtime_profile(tmp_path: Path) -> None:
    raw = _public_config(tmp_path / "workspaces")
    profile = raw["models"]["profiles"][0]  # type: ignore[index]
    profile.update(  # type: ignore[union-attr]
        {
            "provider_id": "dashscope",
            "model": "qwen-vl-exact",
            "allowed_models": ["qwen-vl-exact"],
            "capabilities": {"vision": True, "json_schema": True},
            "allow_capability_overrides": True,
        }
    )

    config = SuiteHarnessConfig.model_validate(raw)
    runtime = config.models.profiles[0].to_runtime()

    assert runtime.capabilities.vision is True
    assert runtime.capabilities.json_schema is True
    assert runtime.allow_capability_overrides is True


def test_model_capabilities_reject_unknown_config_fields(tmp_path: Path) -> None:
    raw = _public_config(tmp_path / "workspaces")
    profile = raw["models"]["profiles"][0]  # type: ignore[index]
    profile["capabilities"] = {"imaginary": True}  # type: ignore[index]

    with pytest.raises(ValidationError, match="imaginary"):
        SuiteHarnessConfig.model_validate(raw)


def test_default_loader_accepts_normal_yaml_syntax(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    public = tmp_path / "suiteharness.yaml"
    private = tmp_path / "suiteharness.secrets.yaml"
    public.write_text(
        """\
config_version: 1
deployment:
  mode: server
  environment: development
  instance_id: acme-main
  tenant_id: acme
customer_bundle:
  customer_bundle_id: acme-products
  version: 1.0.0
  harness_api: ">=0.1,<0.2"
  products:
    - product_id: sales
      version: "==1.0.0"
      config: {}
workspace:
  root: """
        + json.dumps(str(tmp_path / "workspaces"))
        + """
  shared_enabled: true
storage:
  root: """
        + json.dumps(str(tmp_path / "state"))
        + """
sandbox:
  backend: docker
  required: true
  image: registry.example/suiteharness-sandbox:0.1.0
models:
  profiles:
    - profile_id: local-qwen
      provider_id: ollama
      model: qwen3
      base_url: http://ollama:11434/v1
      allow_plain_http: true
  routes:
    - route_id: default
      primary_profile: local-qwen
  default_route: default
web_tools:
  search:
    default_provider: baidu-qianfan
    providers:
      - kind: baidu_qianfan
        credentials_ref: search/baidu-qianfan
  fetch:
    default_route: direct
    routes:
      - kind: direct
        name: direct
channels:
  web:
    enabled: true
    websocket_path: /ws
    credentials_ref: web/default
  feishu:
    enabled: false
""",
        encoding="utf-8",
    )
    private.write_text(
        """\
config_version: 1
web:
  web/default:
    session_signing_key: a-very-long-server-owned-signing-key
services:
  search/baidu-qianfan:
    token: server-search-token
""",
        encoding="utf-8",
    )
    if os.name != "nt":
        private.chmod(0o600)

    loaded = SuiteHarnessConfigLoader().load(public, private)

    assert loaded.config.workspace.root == tmp_path / "workspaces"


def test_loader_does_not_read_or_expand_environment_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public = tmp_path / "suiteharness.yaml"
    private = tmp_path / "suiteharness.secrets.yaml"
    raw = _public_config(tmp_path / "literal")
    raw["deployment"]["tenant_id"] = "acme"  # type: ignore[index]
    _write_json_yaml(public, raw)
    _write_json_yaml(private, _secret_config(), private=True)
    monkeypatch.setenv("SUITEHARNESS_TENANT_ID", "attacker")

    loaded = SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)

    assert loaded.config.deployment.tenant_id == "acme"


def test_accidental_secret_in_public_config_is_rejected_without_echoing_value(
    tmp_path: Path,
) -> None:
    public = tmp_path / "suiteharness.yaml"
    private = tmp_path / "suiteharness.secrets.yaml"
    raw = _public_config(tmp_path / "workspaces")
    raw["channels"]["feishu"]["app_secret"] = "must-not-leak"  # type: ignore[index]
    _write_json_yaml(public, raw)
    _write_json_yaml(private, _secret_config(), private=True)

    with pytest.raises(ConfigLoadError) as captured:
        SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)

    assert "app_secret" in str(captured.value)
    assert "must-not-leak" not in str(captured.value)


def test_personal_mode_and_unprotected_production_sandbox_are_not_representable(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["deployment"]["mode"] = "personal"  # type: ignore[index]
    with pytest.raises(ValidationError):
        SuiteHarnessConfig.model_validate(raw)

    raw = _public_config(tmp_path / "workspaces")
    raw["deployment"]["environment"] = "production"  # type: ignore[index]
    raw["sandbox"] = {
        "backend": "local",
        "required": False,
        "acknowledge_unsafe": True,
    }
    with pytest.raises(ValidationError, match="production deployments require"):
        SuiteHarnessConfig.model_validate(raw)


def test_production_requires_digest_https_origin_and_at_least_one_channel(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["deployment"]["environment"] = "production"  # type: ignore[index]
    raw["channels"]["web"]["allowed_origins"] = ["https://assistant.acme.cn"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="pinned by sha256"):
        SuiteHarnessConfig.model_validate(raw)

    raw["sandbox"]["image"] = "registry.example/suiteharness@sha256:" + "a" * 64  # type: ignore[index]
    parsed = SuiteHarnessConfig.model_validate(raw)
    assert parsed.channels.web.approval_mode == "interactive"

    raw["channels"]["web"]["enabled"] = False  # type: ignore[index]
    with pytest.raises(ValidationError, match="at least one company channel"):
        SuiteHarnessConfig.model_validate(raw)


def test_docker_sandbox_context_is_explicit_and_strict(tmp_path: Path) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["sandbox"]["context"] = "suiteharness-rootless"  # type: ignore[index]
    raw["sandbox"]["require_rootless"] = True  # type: ignore[index]
    parsed = SuiteHarnessConfig.model_validate(raw)
    assert parsed.sandbox.context == "suiteharness-rootless"  # type: ignore[union-attr]
    assert parsed.sandbox.require_rootless is True  # type: ignore[union-attr]

    raw["sandbox"]["require_rootless"] = "true"  # type: ignore[index]
    with pytest.raises(ValidationError, match="require_rootless"):
        SuiteHarnessConfig.model_validate(raw)
    raw["sandbox"]["require_rootless"] = True  # type: ignore[index]

    for unsafe in ("", "bad context", "--host", "bad/context", "x" * 129):
        raw["sandbox"]["context"] = unsafe  # type: ignore[index]
        with pytest.raises(ValidationError, match="context"):
            SuiteHarnessConfig.model_validate(raw)

    raw["sandbox"]["context"] = "suiteharness-rootless"  # type: ignore[index]
    raw["sandbox"]["image"] = "--help@sha256:" + "a" * 64  # type: ignore[index]
    with pytest.raises(ValidationError, match="image"):
        SuiteHarnessConfig.model_validate(raw)

def test_production_cannot_disable_secure_websocket_cookie(tmp_path: Path) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["deployment"]["environment"] = "production"  # type: ignore[index]
    raw["channels"]["web"]["allowed_origins"] = ["https://assistant.acme.cn"]  # type: ignore[index]
    raw["sandbox"]["image"] = "registry.example/suiteharness@sha256:" + "a" * 64  # type: ignore[index]
    raw["channels"]["web"]["session_cookie_secure"] = False  # type: ignore[index]
    with pytest.raises(ValidationError, match="cookies must use Secure"):
        SuiteHarnessConfig.model_validate(raw)


def test_web_authentication_and_protocol_frame_limits_are_hard_bounded(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    web = raw["channels"]["web"]  # type: ignore[index]
    web["authentication_timeout_seconds"] = 60
    web["session_revalidation_interval_seconds"] = 30
    web["max_frame_bytes"] = 1_048_576
    parsed = SuiteHarnessConfig.model_validate(raw)
    assert parsed.channels.web.authentication_timeout_seconds == 60
    assert parsed.channels.web.session_revalidation_interval_seconds == 30
    assert parsed.channels.web.max_frame_bytes == 1_048_576

    web["authentication_timeout_seconds"] = 60.01
    with pytest.raises(ValidationError):
        SuiteHarnessConfig.model_validate(raw)

    for invalid in (True, "10"):
        web["authentication_timeout_seconds"] = invalid
        with pytest.raises(ValidationError):
            SuiteHarnessConfig.model_validate(raw)

    web["authentication_timeout_seconds"] = 10
    for invalid in (True, "30", 0, 300.01):
        web["session_revalidation_interval_seconds"] = invalid
        with pytest.raises(ValidationError):
            SuiteHarnessConfig.model_validate(raw)

    web["session_revalidation_interval_seconds"] = 31
    web["session_lifetime_seconds"] = 30
    with pytest.raises(ValidationError, match="revalidation interval"):
        SuiteHarnessConfig.model_validate(raw)

    web["session_revalidation_interval_seconds"] = 30
    web["max_frame_bytes"] = 2_097_152
    with pytest.raises(ValidationError):
        SuiteHarnessConfig.model_validate(raw)


def test_channel_authorization_timeout_is_strict_and_hard_bounded(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    channels = raw["channels"]  # type: ignore[index]
    channels["authorization_timeout_seconds"] = 60
    parsed = SuiteHarnessConfig.model_validate(raw)
    assert parsed.channels.authorization_timeout_seconds == 60

    for invalid in (True, "10", 0, 60.01):
        channels["authorization_timeout_seconds"] = invalid
        with pytest.raises(ValidationError):
            SuiteHarnessConfig.model_validate(raw)


def test_session_store_limits_are_loaded_only_from_strict_configuration(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    storage = raw["storage"]  # type: ignore[index]
    storage["session_limits"] = {
        "max_sessions": 20,
        "max_transcript_events_per_session": 10,
        "max_transcript_events_total": 100,
        "max_run_claims_per_session": 10,
        "max_run_claims_total": 100,
        "max_checkpoints_per_session": 3,
    }
    parsed = SuiteHarnessConfig.model_validate(raw)
    assert parsed.storage.session_limits.max_sessions == 20
    assert parsed.storage.session_limits.max_checkpoints_per_session == 3

    storage["session_limits"]["max_sessions"] = True  # type: ignore[index]
    with pytest.raises(ValidationError):
        SuiteHarnessConfig.model_validate(raw)


def test_production_rejects_credentials_over_plain_http_model_endpoint(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["deployment"]["environment"] = "production"  # type: ignore[index]
    raw["sandbox"]["image"] = "registry.example/suiteharness@sha256:" + "a" * 64  # type: ignore[index]
    raw["channels"]["web"]["allowed_origins"] = ["https://assistant.acme.cn"]  # type: ignore[index]
    profile = raw["models"]["profiles"][0]  # type: ignore[index]
    profile["credentials_ref"] = "models/private"  # type: ignore[index]

    with pytest.raises(ValidationError, match="carrying credentials must use HTTPS"):
        SuiteHarnessConfig.model_validate(raw)

    profile["base_url"] = "https://model-gateway.acme.cn/v1"  # type: ignore[index]
    profile["allow_plain_http"] = False  # type: ignore[index]
    parsed = SuiteHarnessConfig.model_validate(raw)
    assert parsed.models.profiles[0].base_url.startswith("https://")


def test_feishu_is_non_interactive_read_only_by_default_and_never_deletes(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["channels"] = {
        "web": {"enabled": False},
        "feishu": {
            "enabled": True,
            "app_id": "cli_example",
            "credentials_ref": "feishu/default",
            "writable_roots": [{"space": "product", "path": "exports"}],
        },
    }
    parsed = SuiteHarnessConfig.model_validate(raw)
    assert parsed.channels.feishu.default_access == "read_only"
    assert parsed.channels.feishu.authentication_timeout_seconds == 10
    assert parsed.channels.feishu.event_processing_timeout_seconds == 600
    assert parsed.channels.feishu.event_processing_lease_seconds == 660

    raw["channels"]["feishu"]["authentication_timeout_seconds"] = 60  # type: ignore[index]
    parsed_at_maximum = SuiteHarnessConfig.model_validate(raw)
    assert parsed_at_maximum.channels.feishu.authentication_timeout_seconds == 60
    for invalid in (True, "10", 0, 60.01):
        raw["channels"]["feishu"]["authentication_timeout_seconds"] = invalid  # type: ignore[index]
        with pytest.raises(ValidationError):
            SuiteHarnessConfig.model_validate(raw)
    raw["channels"]["feishu"]["authentication_timeout_seconds"] = 10  # type: ignore[index]
    raw["channels"]["feishu"]["event_processing_timeout_seconds"] = 900  # type: ignore[index]
    raw["channels"]["feishu"]["event_processing_lease_seconds"] = 901  # type: ignore[index]
    custom_processing = SuiteHarnessConfig.model_validate(raw)
    assert custom_processing.channels.feishu.event_processing_timeout_seconds == 900
    assert custom_processing.channels.feishu.event_processing_lease_seconds == 901

    for timeout, lease in ((600, 600), (601, 600), (True, 660), (600, "660")):
        raw["channels"]["feishu"]["event_processing_timeout_seconds"] = timeout  # type: ignore[index]
        raw["channels"]["feishu"]["event_processing_lease_seconds"] = lease  # type: ignore[index]
        with pytest.raises(ValidationError):
            SuiteHarnessConfig.model_validate(raw)
    assert parsed.channels.feishu.interactive_approval is False
    assert parsed.channels.feishu.allow_delete is False

    raw["channels"]["feishu"]["authentication_timeout_seconds"] = 10  # type: ignore[index]
    raw["channels"]["feishu"]["event_processing_timeout_seconds"] = 600  # type: ignore[index]
    raw["channels"]["feishu"]["event_processing_lease_seconds"] = 660  # type: ignore[index]
    raw["channels"]["feishu"]["allow_delete"] = True  # type: ignore[index]
    with pytest.raises(ValidationError):
        SuiteHarnessConfig.model_validate(raw)


def test_loader_requires_matching_credentials_reference(tmp_path: Path) -> None:
    public = tmp_path / "suiteharness.yaml"
    private = tmp_path / "suiteharness.secrets.yaml"
    _write_json_yaml(public, _public_config(tmp_path / "workspaces"))
    _write_json_yaml(private, {"config_version": 1}, private=True)

    with pytest.raises(ConfigLoadError, match="was not found"):
        SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)


def test_mcp_inventory_is_product_scoped_and_credential_refs_are_checked(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["mcp"] = {
        "servers_by_product": {
            "sales": [
                {
                    "server_id": "crm",
                    "transport": "streamable_http",
                    "endpoint": "https://mcp.example.com/rpc",
                    "credential_ref": "mcp/crm",
                }
            ]
        }
    }
    parsed = SuiteHarnessConfig.model_validate(raw)
    assert parsed.mcp.servers_by_product["sales"][0].server_id == "crm"

    public = tmp_path / "suiteharness.yaml"
    private = tmp_path / "suiteharness.secrets.yaml"
    _write_json_yaml(public, raw)
    _write_json_yaml(private, _secret_config(), private=True)
    with pytest.raises(ConfigLoadError, match="service credentials_ref"):
        SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)


def test_mcp_server_ids_cannot_repeat_within_a_product(tmp_path: Path) -> None:
    raw = _public_config(tmp_path / "workspaces")
    server = {
        "server_id": "documents",
        "transport": "stdio",
        "command": "document-mcp",
    }
    raw["mcp"] = {"servers_by_product": {"sales": [server, server]}}
    with pytest.raises(ValidationError, match="unique within product"):
        SuiteHarnessConfig.model_validate(raw)


def test_mcp_channel_access_is_exact_and_defaults_to_deny(tmp_path: Path) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["mcp"] = {
        "servers_by_product": {
            "sales": [
                {
                    "server_id": "crm",
                    "transport": "streamable_http",
                    "endpoint": "https://crm.example.com/rpc",
                },
                {
                    "server_id": "documents",
                    "transport": "stdio",
                    "command": "document-mcp",
                },
            ]
        },
        "channel_access": {
            "web": {
                "sales": {
                    "allow_servers": ["crm"],
                    "allow_tools": {"documents": ["documents.search"]},
                }
            }
        },
    }
    parsed = SuiteHarnessConfig.model_validate(raw)
    rule = parsed.mcp.channel_access.web["sales"]
    assert rule.allow_servers == frozenset({"crm"})
    assert rule.allow_tools == {
        "documents": frozenset({"documents.search"})
    }
    assert parsed.mcp.channel_access.feishu == {}

    defaulted = SuiteHarnessConfig.model_validate(_public_config(tmp_path / "default"))
    assert defaulted.mcp.channel_access.web == {}
    assert defaulted.mcp.channel_access.feishu == {}


def test_mcp_channel_access_rejects_unknown_or_ambiguous_targets(tmp_path: Path) -> None:
    def configured() -> dict[str, object]:
        raw = _public_config(tmp_path / "workspaces")
        raw["mcp"] = {
            "servers_by_product": {
                "sales": [
                    {
                        "server_id": "crm",
                        "transport": "streamable_http",
                        "endpoint": "https://crm.example.com/rpc",
                    }
                ]
            },
            "channel_access": {"web": {}},
        }
        return raw

    raw = configured()
    raw["mcp"]["channel_access"]["web"] = {  # type: ignore[index]
        "support": {"allow_servers": ["crm"]}
    }
    with pytest.raises(ValidationError, match="products without MCP servers"):
        SuiteHarnessConfig.model_validate(raw)

    raw = configured()
    raw["mcp"]["channel_access"]["web"] = {  # type: ignore[index]
        "sales": {"allow_servers": ["unknown"]}
    }
    with pytest.raises(ValidationError, match="unknown or disabled servers"):
        SuiteHarnessConfig.model_validate(raw)

    raw = configured()
    raw["mcp"]["channel_access"]["web"] = {  # type: ignore[index]
        "sales": {
            "allow_servers": ["crm"],
            "allow_tools": {"crm": ["crm.lookup"]},
        }
    }
    with pytest.raises(ValidationError, match="must not overlap"):
        SuiteHarnessConfig.model_validate(raw)

    raw = configured()
    raw["mcp"]["channel_access"] = {  # type: ignore[index]
        "feishu": {"sales": {"allow_tools": {"crm": ["crm.lookup"]}}}
    }
    with pytest.raises(ValidationError, match="Feishu channel"):
        SuiteHarnessConfig.model_validate(raw)


def test_plugin_sources_must_be_explicit_and_beneath_allowlisted_roots(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    plugin_root = tmp_path / "plugins"
    raw["plugins"] = {
        "allowed_roots": [str(plugin_root)],
        "sources": [
            {
                "path": str(plugin_root / "memory-a"),
                "expected_digest": "sha256:" + "a" * 64,
            }
        ],
        "trusted_in_process_plugins": ["memory-a"],
    }
    parsed = SuiteHarnessConfig.model_validate(raw)
    assert parsed.plugins.sources[0].path == plugin_root / "memory-a"

    raw["plugins"]["sources"][0]["path"] = str(tmp_path / "outside")  # type: ignore[index]
    with pytest.raises(ValidationError, match="outside configured allowed_roots"):
        SuiteHarnessConfig.model_validate(raw)


def test_search_provider_id_cannot_disagree_with_its_adapter(tmp_path: Path) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["web_tools"]["search"]["providers"][0]["provider_id"] = "custom"  # type: ignore[index]
    with pytest.raises(ValidationError):
        SuiteHarnessConfig.model_validate(raw)


def test_multi_product_feishu_requires_explicit_conversation_routing(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["customer_bundle"]["products"].append(  # type: ignore[index,union-attr]
        {"product_id": "service", "version": "==1.0.0", "config": {}}
    )
    raw["channels"] = {
        "web": {"enabled": False},
        "feishu": {
            "enabled": True,
            "app_id": "cli_example",
            "credentials_ref": "feishu/default",
        },
    }
    with pytest.raises(ValidationError, match="conversation route"):
        SuiteHarnessConfig.model_validate(raw)

    raw["channels"]["routes"] = [  # type: ignore[index]
        {
            "channel": "feishu",
            "conversation_id": "oc_sales_department",
            "product_id": "sales",
        }
    ]
    parsed = SuiteHarnessConfig.model_validate(raw)
    assert parsed.channels.routes[0].product_id == "sales"


def test_product_specific_configuration_cannot_escape_customer_bundle(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["models"]["product_routes"] = {"unknown-product": "default"}  # type: ignore[index]
    with pytest.raises(ValidationError, match="outside customer_bundle"):
        SuiteHarnessConfig.model_validate(raw)

    raw = _public_config(tmp_path / "workspaces")
    raw["channels"]["agent_ids"] = {"unknown-product": "assistant"}  # type: ignore[index]
    with pytest.raises(ValidationError, match="outside customer_bundle"):
        SuiteHarnessConfig.model_validate(raw)


def test_feishu_webhook_requires_both_verification_secrets(tmp_path: Path) -> None:
    public = tmp_path / "suiteharness.yaml"
    private = tmp_path / "suiteharness.secrets.yaml"
    raw = _public_config(tmp_path / "workspaces")
    raw["channels"] = {
        "web": {"enabled": False},
        "feishu": {
            "enabled": True,
            "transport": "webhook",
            "app_id": "cli_example",
            "credentials_ref": "feishu/default",
        },
    }
    _write_json_yaml(public, raw)
    secrets = _secret_config()
    secrets["feishu"] = {
        "feishu/default": {
            "app_secret": "app-secret",
            "verification_token": "verification-token",
        }
    }
    _write_json_yaml(private, secrets, private=True)

    with pytest.raises(ConfigLoadError, match="encrypt_key"):
        SuiteHarnessConfigLoader(yaml_decoder=json.loads).load(public, private)


def test_storage_databases_are_distinct_and_search_cannot_use_browser_get_route(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["storage"]["runtime_database"] = "sessions.sqlite3"  # type: ignore[index]
    with pytest.raises(ValidationError, match="different files"):
        SuiteHarnessConfig.model_validate(raw)

    raw = _public_config(tmp_path / "workspaces")
    raw["web_tools"]["fetch"] = {  # type: ignore[index]
        "default_route": "browser",
        "routes": [
            {
                "kind": "browser_worker",
                "name": "browser",
                "endpoint": "https://browser-worker.internal",
                "credentials_ref": "browser/default",
            }
        ],
    }
    with pytest.raises(ValidationError, match="GET-only browser_worker"):
        SuiteHarnessConfig.model_validate(raw)


def test_shared_workspace_is_off_by_default_and_access_is_product_scoped(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["workspace"] = {"root": str(tmp_path / "default-workspaces")}
    parsed = SuiteHarnessConfig.model_validate(raw)
    assert parsed.workspace.shared_enabled is False
    assert parsed.workspace.shared_access_by_product == {}

    raw["workspace"] = {
        "root": str(tmp_path / "explicit-workspaces"),
        "shared_enabled": False,
        "shared_access_by_product": {"sales": "read_only"},
    }
    with pytest.raises(ValidationError, match="requires shared_enabled"):
        SuiteHarnessConfig.model_validate(raw)

    raw["workspace"]["shared_enabled"] = True  # type: ignore[index]
    raw["workspace"]["shared_access_by_product"] = {"unknown": "read_write"}  # type: ignore[index]
    with pytest.raises(ValidationError, match="outside customer_bundle"):
        SuiteHarnessConfig.model_validate(raw)


def test_feishu_shared_write_requires_explicit_product_read_write_access(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["workspace"] = {
        "root": str(tmp_path / "workspaces"),
        "shared_enabled": True,
        "shared_access_by_product": {"sales": "read_only"},
    }
    raw["channels"]["feishu"] = {  # type: ignore[index]
        "enabled": True,
        "app_id": "cli-example",
        "credentials_ref": "feishu/default",
        "writable_roots": [{"space": "shared", "path": "exports"}],
    }
    with pytest.raises(ValidationError, match="read_write product access"):
        SuiteHarnessConfig.model_validate(raw)

    raw["workspace"]["shared_access_by_product"] = {"sales": "read_write"}  # type: ignore[index]
    parsed = SuiteHarnessConfig.model_validate(raw)
    assert parsed.workspace.shared_access_by_product["sales"] == "read_write"


def test_product_egress_allowlists_validate_products_and_configured_routes(
    tmp_path: Path,
) -> None:
    raw = _public_config(tmp_path / "workspaces")
    raw["sandbox"]["network"] = {  # type: ignore[index]
        "egress_profiles": {"network-a": "docker-network-a"},
        "allowed_profiles_by_product": {"sales": ["missing"]},
    }
    with pytest.raises(ValidationError, match="unknown egress profiles"):
        SuiteHarnessConfig.model_validate(raw)

    raw = _public_config(tmp_path / "workspaces")
    raw["web_tools"]["fetch_routes_by_product"] = {"unknown": ["direct"]}  # type: ignore[index]
    with pytest.raises(ValidationError, match="outside customer_bundle"):
        SuiteHarnessConfig.model_validate(raw)

    raw = _public_config(tmp_path / "workspaces")
    raw["web_tools"]["search_providers_by_product"] = {"sales": ["missing"]}  # type: ignore[index]
    with pytest.raises(ValidationError, match="unknown providers"):
        SuiteHarnessConfig.model_validate(raw)

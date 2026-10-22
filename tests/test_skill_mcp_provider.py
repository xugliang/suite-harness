"""Pinned MCP application-contract tests; fake client, no network or credentials."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import asdict

import pytest

from suiteharness.mcp.manager import McpClientManager
from suiteharness.mcp.models import (
    CallToolResult,
    ClientCapabilities,
    McpConnectionState,
    TextContent,
)
from suiteharness.runtime.scopes import RequestScope, ScopePath
from suiteharness.skills import (
    BoundMcpSkillClient,
    McpSkillProvider,
    SkillAccess,
    SkillDescriptor,
    SkillError,
    mcp_skill_provider_from_config,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def descriptor(name: str = "work", **kwargs) -> SkillDescriptor:
    return SkillDescriptor(**{
        "skill_id": f"demo.{name}", "name": name, "description": f"Use {name}",
        "version": "1.0.0", "source_sha256": digest(f"source-{name}"), **kwargs,
    })


def wire(item: SkillDescriptor) -> dict:
    return {**asdict(item), "includes": list(item.includes)}


def access(*, source: str = "model", employee: str = "alice", product: str = "demo") -> SkillAccess:
    return SkillAccess(RequestScope(ScopePath.product("company", product), employee), source=source)


class FakeMcp:
    product_id = "demo"
    tenant_id = "company"

    def __init__(self, descriptors, documents):
        self.descriptors = descriptors
        self.documents = documents
        self.calls = []
        self.error = None
        self.response_override = None
        self.text_response = False

    async def call_tool(self, scope, name, arguments):
        self.calls.append((scope, name, arguments))
        if self.error is not None:
            raise self.error
        if self.response_override is not None:
            raw = self.response_override
        elif name == "skills.list":
            raw = {"package_id": "approved-package", "skills": [wire(d) for d in self.descriptors]}
        else:
            item = next(d for d in self.descriptors if d.skill_id == arguments["skill_id"])
            body = self.documents[(item.skill_id, arguments["resource_id"])]
            raw = {
                "package_id": "approved-package", "descriptor": wire(item),
                "resource_id": arguments["resource_id"], "instructions": body,
                "instructions_sha256": digest(body), "dependencies": list(item.includes),
            }
        if self.text_response:
            return CallToolResult(content=(TextContent(text=json.dumps(raw)),))
        return CallToolResult(content=(), structuredContent=raw)


def provider(client, manifest, resources, **kwargs):
    return McpSkillProvider(
        client, namespace="demo", package_id="approved-package", manifest=tuple(manifest),
        resource_hashes=resources, **kwargs,
    )


def test_mcp_roundtrip_and_identity_are_preserved() -> None:
    work = descriptor()
    client = FakeMcp([work], {("demo.work", "instructions"): "Complete rules"})
    p = provider(client, [work], {"demo.work": {"instructions": digest("Complete rules")}})

    async def exercise():
        assert await p.list_skills(access()) == (work,)
        document = await p.read_skill(access(), "demo.work")
        assert document.source == "mcp"
        assert document.instructions == "Complete rules"

    asyncio.run(exercise())
    assert client.calls[-1][0].principal_id == "alice"
    assert client.calls[-1][2] == {
        "package_id": "approved-package", "skill_id": "demo.work", "resource_id": "instructions",
        "version": "1.0.0", "source_sha256": work.source_sha256,
    }
    assert p.static_manifest() == (work,)


def test_mcp_text_json_is_supported_but_never_fetched_as_a_url() -> None:
    work = descriptor()
    client = FakeMcp([work], {("demo.work", "instructions"): "Complete rules"})
    client.text_response = True
    p = provider(client, [work], {"demo.work": {"instructions": digest("Complete rules")}})
    assert asyncio.run(p.read_skill(access(), "demo.work")).instructions == "Complete rules"
    client.response_override = {"url": "https://example.invalid/untrusted.md"}
    with pytest.raises(SkillError):
        asyncio.run(p.read_skill(access(), "demo.work"))


def test_remote_hash_cannot_self_approve_changed_instructions() -> None:
    work = descriptor()
    client = FakeMcp([work], {("demo.work", "instructions"): "Unapproved replacement"})
    p = provider(client, [work], {"demo.work": {"instructions": digest("Approved rules")}})
    with pytest.raises(SkillError) as caught:
        asyncio.run(p.read_skill(access(), "demo.work"))
    assert caught.value.code == "skill_content_changed"


def test_remote_cannot_add_or_override_manifest() -> None:
    work = descriptor()
    client = FakeMcp([work, descriptor("unregistered")], {})
    p = provider(client, [work], {"demo.work": {"instructions": digest("Approved")}})
    with pytest.raises(SkillError):
        asyncio.run(p.list_skills(access()))
    client.descriptors = [descriptor(description="Replacement")]
    with pytest.raises(SkillError) as caught:
        asyncio.run(p.list_skills(access()))
    assert caught.value.code == "skill_content_changed"


def test_remote_dependency_policy_and_product_scope_checked_before_network() -> None:
    common = descriptor("common", model_invocable=False)
    work = descriptor(includes=("demo.common",))
    client = FakeMcp([common, work], {("demo.work", "instructions"): "Common\n\nWork"})
    locks = {
        "demo.common": {"instructions": digest("Common")},
        "demo.work": {"instructions": digest("Common\n\nWork")},
    }
    p = provider(client, [common, work], locks, access_policy=lambda _a, d, _r: d.name != "common")
    with pytest.raises(SkillError):
        asyncio.run(p.read_skill(access(), "demo.work"))
    assert client.calls == []
    with pytest.raises(SkillError):
        asyncio.run(p.read_skill(access(product="other"), "demo.work"))
    assert client.calls == []


def test_remote_unavailable_does_not_return_an_old_success_or_leak_error_secrets() -> None:
    work = descriptor()
    client = FakeMcp([work], {("demo.work", "instructions"): "Rules"})
    p = provider(client, [work], {"demo.work": {"instructions": digest("Rules")}})
    assert asyncio.run(p.read_skill(access(), "demo.work"))
    client.error = RuntimeError("Authorization: secret-token")
    with pytest.raises(SkillError) as caught:
        asyncio.run(p.read_skill(access(), "demo.work"))
    assert caught.value.code == "skill_read_failed"
    assert "secret-token" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert len(client.calls) == 2  # No hidden retries or stale cache fallback.


def test_remote_resources_must_be_explicitly_pinned_and_reply_is_workflow_only() -> None:
    work = descriptor()
    client = FakeMcp([work], {("demo.work", "reply"): "Only current result"})
    with pytest.raises(SkillError):
        provider(client, [work], {})
    p = provider(client, [work], {
        "demo.work": {"instructions": digest("Rules"), "reply": digest("Only current result")},
    })
    with pytest.raises(SkillError):
        asyncio.run(p.read_skill(access(), "demo.work", resource_id="reply"))
    assert client.calls == []
    assert asyncio.run(p.read_skill(access(source="workflow"), "demo.work", resource_id="reply"))


def test_remote_timeout_is_bounded_without_retry() -> None:
    work = descriptor()

    class SlowMcp(FakeMcp):
        async def call_tool(self, scope, name, arguments):
            self.calls.append((scope, name, arguments))
            await asyncio.sleep(1)

    client = SlowMcp([work], {})
    p = provider(client, [work], {"demo.work": {"instructions": digest("Rules")}}, timeout_seconds=0.01)
    with pytest.raises(SkillError) as caught:
        asyncio.run(p.read_skill(access(), "demo.work"))
    assert caught.value.retryable
    assert len(client.calls) == 1


def test_invalid_remote_unicode_is_a_structured_error() -> None:
    work = descriptor()
    client = FakeMcp([work], {})
    client.response_override = {
        "package_id": "approved-package", "descriptor": wire(work),
        "resource_id": "instructions", "instructions": "\ud800",
        "instructions_sha256": "0" * 64, "dependencies": [],
    }
    p = provider(client, [work], {"demo.work": {"instructions": digest("Rules")}})
    with pytest.raises(SkillError) as caught:
        asyncio.run(p.read_skill(access(), "demo.work"))
    assert caught.value.code == "skill_invalid"


def test_existing_manager_signature_and_actual_lookup_are_used() -> None:
    # This intentionally checks the real public manager contract, rather than a
    # fake manager with an invented positional signature.
    assert tuple(inspect.signature(McpClientManager.call_tool).parameters) == (
        "self", "scope", "server_id", "tool_name", "arguments",
    )
    work = descriptor()
    client = FakeMcp([work], {("demo.work", "instructions"): "Rules"})
    client.state = McpConnectionState.READY
    # No factory method can be called in this test. We inject a fake, already
    # connected peer into the actual manager's test state, then exercise its
    # unmodified call_tool -> client(scope, server_id) -> client.call_tool chain.
    manager = McpClientManager(object(), client_capabilities=ClientCapabilities())
    manager._clients[("company", "demo", "skill-service")] = client
    p = McpSkillProvider.from_manager(
        manager, tenant_id="company", product_id="demo", server_id="skill-service",
        package_id="approved-package", manifest=(work,),
        resource_hashes={"demo.work": {"instructions": digest("Rules")}},
    )
    request = access(employee="bob")

    async def exercise():
        assert await p.list_skills(request) == (work,)
        assert (await p.read_skill(request, "demo.work")).instructions == "Rules"

    asyncio.run(exercise())
    assert len(client.calls) == 2
    assert all(call[0] is request.scope for call in client.calls)
    assert client.calls[-1][1] == "skills.read"
    # The real manager does not resolve the same server for another product.
    with pytest.raises(LookupError):
        manager.client(access(product="other").scope, "skill-service")


def test_manager_binding_does_not_allow_server_or_product_selection_by_model() -> None:
    work = descriptor()
    manager = McpClientManager(object(), client_capabilities=ClientCapabilities())
    binding = BoundMcpSkillClient(manager, "company", "demo", "approved-server")
    with pytest.raises(SkillError) as caught:
        asyncio.run(binding.call_tool(access(product="other").scope, "skills.read", {}))
    assert caught.value.code == "skill_not_available"
    p = McpSkillProvider.from_manager(
        manager, tenant_id="company", product_id="demo", server_id="not-connected",
        package_id="approved-package", manifest=(work,),
        resource_hashes={"demo.work": {"instructions": digest("Rules")}},
    )
    with pytest.raises(SkillError) as caught:
        asyncio.run(p.read_skill(access(), "demo.work"))
    assert caught.value.code == "skill_read_failed"
    assert manager._clients == {}  # No connection, fallback or auto-creation.


def test_strict_config_factory_accepts_only_a_connection_reference_and_lockfile() -> None:
    work = descriptor()
    client = FakeMcp([work], {("demo.work", "instructions"): "Rules"})
    client.state = McpConnectionState.READY
    manager = McpClientManager(object(), client_capabilities=ClientCapabilities())
    manager._clients[("company", "demo", "skill-service")] = client
    config = {
        "server_id": "skill-service", "package_id": "approved-package",
        "manifest": [wire(work)], "resource_hashes": {"demo.work": {"instructions": digest("Rules")}},
    }
    p = mcp_skill_provider_from_config(manager, tenant_id="company", product_id="demo", config=config)
    assert asyncio.run(p.read_skill(access(), "demo.work")).instructions == "Rules"
    for key in ("url", "credential_ref", "principal_id", "roles"):
        with pytest.raises(SkillError):
            mcp_skill_provider_from_config(
                manager, tenant_id="company", product_id="demo", config={**config, key: "unapproved"},
            )
    for key, value in (("timeout_seconds", "10"), ("list_tool", 1), ("manifest", {})):
        with pytest.raises(SkillError):
            mcp_skill_provider_from_config(
                manager, tenant_id="company", product_id="demo", config={**config, key: value},
            )

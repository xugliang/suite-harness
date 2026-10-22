import asyncio

import pytest

from suiteharness.channels.policy import CompanyChannelAuthorizationPolicy
from suiteharness.execution import RunRequest, ToolAuthorization, ToolEffect, ToolSpec
from suiteharness.execution.in_memory import (
    InMemoryCapabilityAuthority,
    InMemoryToolRegistry,
)
from suiteharness.runtime import RequestScope, ScopePath
from suiteharness.server.access import ConfiguredGrantIssuer, ToolAccessError, ToolAccessTemplate


EXTERNAL_SPEC = ToolSpec(
    name="product.assessment.assign",
    description="Create an assessment and send it to an employee.",
    effects=frozenset({ToolEffect.WRITE, ToolEffect.EXTERNAL}),
    required_capabilities=frozenset({"product.assessment.write"}),
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)


async def _handler(context, arguments):
    del context, arguments
    return {"status": "sent"}


def _scope() -> RequestScope:
    return RequestScope(
        path=ScopePath.agent("company", "product", "default", "session-1"),
        principal_id="employee-1",
        channel_id="feishu",
    )


def test_feishu_external_alias_grants_an_exact_non_read_tool() -> None:
    async def check() -> None:
        authority = InMemoryCapabilityAuthority()
        registry = InMemoryToolRegistry()
        registration = registry.register(
            ScopePath.product("company", "product"),
            EXTERNAL_SPEC,
            _handler,
        )
        issuer = ConfiguredGrantIssuer(
            registry=registry,
            authority=authority,
            templates=(
                ToolAccessTemplate(
                    channel_id="feishu",
                    product_id="product",
                    external_aliases=(EXTERNAL_SPEC.name,),
                ),
            ),
        )

        async with issuer.lease(_scope()) as issued:
            assert issued.read_only is False
            assert issued.grant.tool_identities == frozenset({registration.identity})
            assert issued.grant.capabilities == frozenset(
                {"product.assessment.write"}
            )

    asyncio.run(check())


def test_external_alias_rejects_a_tool_without_write_and_external_effects() -> None:
    async def check() -> None:
        authority = InMemoryCapabilityAuthority()
        registry = InMemoryToolRegistry()
        registry.register(
            ScopePath.product("company", "product"),
            ToolSpec(
                name="product.read",
                effects=frozenset({ToolEffect.READ}),
                input_schema={"type": "object"},
            ),
            _handler,
        )
        issuer = ConfiguredGrantIssuer(
            registry=registry,
            authority=authority,
            templates=(
                ToolAccessTemplate(
                    channel_id="feishu",
                    product_id="product",
                    external_aliases=("product.read",),
                ),
            ),
        )

        with pytest.raises(ToolAccessError, match="external"):
            async with issuer.lease(_scope()):
                pass

    asyncio.run(check())


def test_feishu_policy_allows_only_product_scoped_external_writes() -> None:
    scope = _scope()
    request = RunRequest(
        run_id="run-1",
        scope=scope,
        grant_id="grant-1",
        input="assign",
    )
    product_identity = InMemoryToolRegistry()
    registration = product_identity.register(
        ScopePath.product("company", "product"),
        EXTERNAL_SPEC,
        _handler,
    )
    policy = CompanyChannelAuthorizationPolicy()

    assert (
        policy.evaluate(request, registration.identity, EXTERNAL_SPEC, {})
        is ToolAuthorization.ALLOW
    )
    ordinary_write = EXTERNAL_SPEC.model_copy(
        update={
            "name": "product.assessment.local-write",
            "effects": frozenset({ToolEffect.WRITE}),
        }
    )
    ordinary_registration = product_identity.register(
        ScopePath.product("company", "product"),
        ordinary_write,
        _handler,
    )
    assert (
        policy.evaluate(
            request,
            ordinary_registration.identity,
            ordinary_write,
            {},
        )
        is ToolAuthorization.DENY
    )

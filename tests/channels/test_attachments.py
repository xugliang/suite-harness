from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from pydantic import ValidationError

from suiteharness.channels import (
    AttachmentContentBuilder,
    ChannelAttachment,
    ResolvedAttachment,
)
from suiteharness.channels.websocket.models import MessageFrame
from suiteharness.models import ImageContent, TextContent
from suiteharness.runtime import (
    RequestScope,
    ResourceAccessDenied,
    ResourceRef,
    ScopePath,
    require_resource_access,
)


def scope(principal_id: str = "operator-a") -> RequestScope:
    return RequestScope(
        path=ScopePath.agent("company", "documents", "assistant", "session"),
        principal_id=principal_id,
    )


class FakeAccess:
    def __init__(self) -> None:
        self.allowed = True
        self.calls: list[tuple[str, str, str]] = []

    async def authorize(self, request_scope, resource, *, action):  # type: ignore[no-untyped-def]
        self.calls.append((request_scope.principal_id, resource.resource_id, action))
        return self.allowed and request_scope.principal_id == "operator-a"


class FakeResolver:
    def __init__(self, content: bytes = b"synthetic bytes", media_type: str = "text/plain") -> None:
        self.calls: list[str] = []
        self.value = ResolvedAttachment(
            attachment_id="attachment-1",
            tenant_id="company",
            product_id="documents",
            principal_id="operator-a",
            media_type=media_type,
            content=content,
        )

    async def resolve(self, request_scope, attachment_id):  # type: ignore[no-untyped-def]
        self.calls.append(attachment_id)
        return self.value


def reference() -> tuple[ChannelAttachment, ...]:
    return (ChannelAttachment(attachment_id="attachment-1", media_type="image/png", size_bytes=1),)


def test_text_and_image_content_come_only_from_authorized_server_resolver() -> None:
    resolver = FakeResolver()
    access = FakeAccess()
    builder = AttachmentContentBuilder(resolver, access)
    content = asyncio.run(builder.build(scope(), reference()))
    assert content == (TextContent(text="synthetic bytes"),)
    assert access.calls == [("operator-a", "attachment-1", "read")]
    resolver.value = replace(resolver.value, media_type="image/png", content=b"synthetic image")
    image_parts = asyncio.run(builder.build(scope(), reference()))
    assert isinstance(image_parts[0], ImageContent)
    assert image_parts[0].data_base64 == "c3ludGhldGljIGltYWdl"
    assert image_parts[0].url is None


def test_denied_principal_never_reaches_storage() -> None:
    resolver = FakeResolver()
    with pytest.raises(ResourceAccessDenied, match="resource not found"):
        asyncio.run(
            AttachmentContentBuilder(resolver, FakeAccess()).build(
                scope("operator-b"),
                reference(),
            )
        )
    assert resolver.calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("principal_id", "operator-b"),
        ("tenant_id", "foreign"),
        ("product_id", "foreign"),
        ("attachment_id", "foreign"),
    ],
)
def test_resolver_cannot_return_another_scope_or_object(field: str, value: str) -> None:
    resolver = FakeResolver()
    resolver.value = replace(resolver.value, **{field: value})
    with pytest.raises(ResourceAccessDenied):
        asyncio.run(AttachmentContentBuilder(resolver, FakeAccess()).build(scope(), reference()))


def test_authorization_is_repeated_and_not_cached_across_calls() -> None:
    resolver, access = FakeResolver(), FakeAccess()
    builder = AttachmentContentBuilder(resolver, access)
    asyncio.run(builder.build(scope(), reference()))
    access.allowed = False
    with pytest.raises(ResourceAccessDenied):
        asyncio.run(builder.build(scope(), reference()))
    assert resolver.calls == ["attachment-1"]


def test_true_byte_limits_override_client_size_metadata() -> None:
    builder = AttachmentContentBuilder(
        FakeResolver(content=b"123456"), FakeAccess(), max_attachment_bytes=5
    )
    with pytest.raises(ValueError, match="byte limit"):
        asyncio.run(builder.build(scope(), reference()))


@pytest.mark.parametrize("value", [True, 1.5, "8"])
def test_attachment_count_limit_requires_a_real_integer(value: object) -> None:
    with pytest.raises(ValueError, match="integers"):
        AttachmentContentBuilder(FakeResolver(), FakeAccess(), max_attachments=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("action,timeout", [(None, 10), ("read", True), ("read", "10")])
def test_resource_authorization_parameters_are_strict(
    action: object,
    timeout: object,
) -> None:
    with pytest.raises(ValueError, match="invalid resource authorization"):
        asyncio.run(
            require_resource_access(
                FakeAccess(),
                scope(),
                ResourceRef("company", "documents", "document", "one"),
                action=action,  # type: ignore[arg-type]
                timeout_seconds=timeout,  # type: ignore[arg-type]
            )
        )


def test_duplicates_and_unsupported_media_are_rejected() -> None:
    builder = AttachmentContentBuilder(
        FakeResolver(media_type="application/octet-stream"), FakeAccess()
    )
    with pytest.raises(ValueError, match="unsupported"):
        asyncio.run(builder.build(scope(), reference()))
    with pytest.raises(ValueError, match="duplicate"):
        asyncio.run(builder.build(scope(), reference() * 2))


def test_cross_product_resource_is_rejected_before_authorizer() -> None:
    access = FakeAccess()
    with pytest.raises(ResourceAccessDenied):
        asyncio.run(
            require_resource_access(
                access, scope(), ResourceRef("company", "foreign", "document", "one"), action="read"
            )
        )
    assert access.calls == []


def test_authorizer_errors_fail_closed_and_do_not_expose_internal_message() -> None:
    class BrokenAccess:
        async def authorize(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("private-storage-hostname")

    with pytest.raises(ResourceAccessDenied, match="^resource not found$"):
        asyncio.run(
            require_resource_access(
                BrokenAccess(),
                scope(),
                ResourceRef("company", "documents", "document", "one"),
                action="read",
            )
        )


def test_websocket_allows_attachment_only_but_rejects_empty_message() -> None:
    common = {"type": "message", "client_message_id": "one", "conversation_id": "two"}
    assert MessageFrame(**common, attachments=reference()).text == ""
    with pytest.raises(ValidationError, match="requires"):
        MessageFrame(**common)
    with pytest.raises(ValidationError, match="NUL"):
        MessageFrame(**common, text="\x00", attachments=reference())

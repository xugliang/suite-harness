"""Resolve untrusted attachment IDs only through server-owned authorization and storage."""

from __future__ import annotations

import asyncio
import base64
import math
from dataclasses import dataclass, field
from typing import Protocol

from suiteharness.models import ImageContent, TextContent
from suiteharness.runtime.resources import (
    ResourceAccessDenied,
    ResourceAuthorizer,
    ResourceRef,
    require_resource_access,
)
from suiteharness.runtime.scopes import RequestScope

from .models import ChannelAttachment


@dataclass(frozen=True, slots=True)
class ResolvedAttachment:
    """Server-loaded bytes bound to the principal authorized for this resolution."""

    attachment_id: str
    tenant_id: str
    product_id: str
    principal_id: str
    media_type: str
    content: bytes = field(repr=False)


class AttachmentResolver(Protocol):
    async def resolve(self, scope: RequestScope, attachment_id: str) -> ResolvedAttachment:
        """Recheck ownership in the storage operation and return validated stored media.

        Never fetch a client-supplied URL or file path. Upload handlers must validate
        decoded image format/dimensions before putting bytes into this store.
        """
        ...


@dataclass(frozen=True, slots=True)
class AttachmentContentBuilder:
    resolver: AttachmentResolver
    authorizer: ResourceAuthorizer
    max_attachments: int = 8
    max_attachment_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 20 * 1024 * 1024
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        limits = (self.max_attachments, self.max_attachment_bytes, self.max_total_bytes)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in limits):
            raise ValueError("attachment limits must be integers")
        if min(limits) < 1:
            raise ValueError("attachment limits must be positive")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int | float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("attachment timeout must be positive")

    async def build(
        self, scope: RequestScope, attachments: tuple[ChannelAttachment, ...]
    ) -> tuple[ImageContent | TextContent, ...]:
        if len(attachments) > self.max_attachments:
            raise ValueError("too many attachments")
        ids = [item.attachment_id for item in attachments]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate attachment references")
        result: list[ImageContent | TextContent] = []
        total = 0
        for reference in attachments:
            await require_resource_access(
                self.authorizer,
                scope,
                ResourceRef(
                    scope.tenant_id, scope.product_id, "attachment", reference.attachment_id
                ),
                action="read",
                timeout_seconds=self.timeout_seconds,
            )
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    resolved = await self.resolver.resolve(scope, reference.attachment_id)
            except Exception:
                raise ResourceAccessDenied("resource not found") from None
            if not isinstance(resolved, ResolvedAttachment) or (
                resolved.attachment_id != reference.attachment_id
                or resolved.tenant_id != scope.tenant_id
                or resolved.product_id != scope.product_id
                or resolved.principal_id != scope.principal_id
            ):
                raise ResourceAccessDenied("resource not found")
            if not isinstance(resolved.content, bytes) or not resolved.content:
                raise ValueError("attachment content must be non-empty bytes")
            size = len(resolved.content)
            total += size
            if size > self.max_attachment_bytes or total > self.max_total_bytes:
                raise ValueError("attachment content exceeds byte limit")
            # Client metadata is never the source of truth for type or size.
            media_type = resolved.media_type.lower()
            if media_type == "text/plain":
                result.append(TextContent(text=resolved.content.decode("utf-8", errors="strict")))
            elif media_type in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                result.append(
                    ImageContent(
                        mime_type=media_type,
                        data_base64=base64.b64encode(resolved.content).decode("ascii"),
                    )
                )
            else:
                raise ValueError("unsupported attachment media type")
        return tuple(result)

"""Stable errors emitted by the model gateway."""

from __future__ import annotations

from enum import Enum


class ModelErrorCode(str, Enum):
    PROFILE_NOT_FOUND = "profile_not_found"
    PROVIDER_NOT_FOUND = "provider_not_found"
    CREDENTIAL_MISSING = "credential_missing"
    AUTHENTICATION = "authentication"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMIT = "rate_limit"
    INVALID_REQUEST = "invalid_request"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    RESPONSE_INVALID = "response_invalid"
    UNSUPPORTED_FEATURE = "unsupported_feature"
    CONFIGURATION = "configuration"


class ModelProviderError(RuntimeError):
    """A redacted, provider-neutral model failure."""

    def __init__(
        self,
        code: ModelErrorCode,
        message: str,
        *,
        provider_id: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.provider_id = provider_id
        self.status_code = status_code
        self.retryable = retryable

"""Initial file-loader-friendly secret resolver."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import SecretStr


class MappingSecretResolver:
    """Resolve already-loaded secrets without exposing them through repr."""

    def __init__(self, values: Mapping[str, SecretStr | str]) -> None:
        self._values = {
            key: value if isinstance(value, SecretStr) else SecretStr(value)
            for key, value in values.items()
        }

    def resolve(self, reference: str) -> SecretStr:
        try:
            return self._values[reference]
        except KeyError as exc:
            raise KeyError(f"secret reference {reference!r} was not found") from exc

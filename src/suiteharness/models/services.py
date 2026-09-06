"""Root-owned model services exposed to trusted product activators."""

from suiteharness.models.gateway import ModelGateway
from suiteharness.runtime.scopes import ScopeKind, ServiceKey

# Products may resolve the company-configured gateway during installation, but
# cannot replace it at tenant or product scope. Calls still require an
# administrator-declared route, profile, endpoint and credential reference.
MODEL_GATEWAY = ServiceKey(
    "models.gateway",
    ModelGateway,
    ScopeKind.ROOT,
)


__all__ = ["MODEL_GATEWAY"]

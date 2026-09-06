"""Runtime building blocks for the scoped plugin kernel."""

from importlib import import_module

from suiteharness.runtime.bundle import (
    Bundle,
    BundleActivation,
    BundleInstallContext,
    BundleInstallError,
    BundlePlan,
    BundleResolutionCode,
    BundleResolutionError,
    install_bundle_plan,
    resolve_bundle_plan,
)
from suiteharness.runtime.customer_bundle import (
    Customer360Mode,
    Customer360SharingManifest,
    Customer360SharingPlan,
    Customer360SharingRule,
    CustomerBundleManifest,
    CustomerBundlePlan,
    CustomerBundleResolutionCode,
    CustomerBundleResolutionError,
    CustomerProductSelection,
    ProductCatalog,
    ResolvedCustomerProduct,
    resolve_customer_bundle,
)
from suiteharness.runtime.descriptor import (
    BundleManifest,
    BundleRequirement,
    ProductDescriptor,
)
from suiteharness.runtime.effects import EffectCleanupError, EffectScope
from suiteharness.runtime.resources import (
    AuthorizedResource,
    ResourceAccessDenied,
    ResourceAuthorizer,
    ResourceRef,
    require_resource_access,
)
from suiteharness.runtime.scopes import (
    AgentContext,
    ProductContext,
    RequestScope,
    RootContext,
    ScopeKind,
    ScopePath,
    ServiceBindingError,
    ServiceBindings,
    ServiceKey,
    TenantScope,
)

_KERNEL_EXPORTS = frozenset(
    {
        "ActivatedCustomerBundle",
        "CustomerBundleActivationCode",
        "CustomerBundleActivationError",
        "HarnessKernel",
        "PreparedProduct",
        "ProductActivationContext",
        "ProductActivator",
        "ScopedToolRegistry",
    }
)


def __getattr__(name: str) -> object:
    """Load the execution-aware composer lazily to avoid package import cycles."""

    if name not in _KERNEL_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module("suiteharness.runtime.kernel"), name)
    globals()[name] = value
    return value

__all__ = [
    "ActivatedCustomerBundle",
    "AgentContext",
    "AuthorizedResource",
    "Bundle",
    "BundleActivation",
    "BundleInstallContext",
    "BundleInstallError",
    "BundleManifest",
    "BundlePlan",
    "BundleRequirement",
    "BundleResolutionCode",
    "BundleResolutionError",
    "Customer360Mode",
    "Customer360SharingManifest",
    "Customer360SharingPlan",
    "Customer360SharingRule",
    "CustomerBundleManifest",
    "CustomerBundlePlan",
    "CustomerBundleActivationCode",
    "CustomerBundleActivationError",
    "CustomerBundleResolutionCode",
    "CustomerBundleResolutionError",
    "CustomerProductSelection",
    "EffectCleanupError",
    "EffectScope",
    "HarnessKernel",
    "ProductContext",
    "ProductActivationContext",
    "ProductActivator",
    "ProductCatalog",
    "ProductDescriptor",
    "PreparedProduct",
    "RequestScope",
    "ResourceAccessDenied",
    "ResourceAuthorizer",
    "ResourceRef",
    "ResolvedCustomerProduct",
    "RootContext",
    "ScopeKind",
    "ScopePath",
    "ScopedToolRegistry",
    "ServiceBindingError",
    "ServiceBindings",
    "ServiceKey",
    "TenantScope",
    "install_bundle_plan",
    "resolve_bundle_plan",
    "resolve_customer_bundle",
    "require_resource_access",
]

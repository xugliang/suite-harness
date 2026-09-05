"""Loader for explicitly trusted, verified in-process Python source plugins."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import re
import sys
from types import ModuleType

from suiteharness.plugins.discovery import PluginDiscovery, VerifiedPluginSource
from suiteharness.plugins.errors import PluginErrorCode, PluginLifecycleError
from suiteharness.plugins.models import TrustMode
from suiteharness.plugins.protocols import PluginRuntime, TrustedPluginLoader

_SAFE_NAMESPACE = re.compile(r"[^A-Za-z0-9_]")
_ROOT_NAMESPACE = "_suiteharness_source_plugins"


def _within(path: str, root: str) -> bool:
    from pathlib import Path

    try:
        Path(path).resolve(strict=True).relative_to(Path(root).resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


class _NamespaceRuntime:
    def __init__(self, runtime: PluginRuntime, namespace: str) -> None:
        self._runtime = runtime
        self._namespace = namespace

    async def prepare(self, context):
        return await self._runtime.prepare(context)

    async def close(self) -> None:
        try:
            await self._runtime.close()
        finally:
            PythonEntrypointLoader._remove_namespace(self._namespace)


class PythonEntrypointLoader(TrustedPluginLoader):
    """Import under an artifact-specific namespace after re-verification.

    This is an engineering boundary for administrator-trusted Python.  It is not
    a sandbox: trusted modules can import Python libraries and access their host
    process.  Untrusted plugins must use an isolated worker or MCP launcher.
    """

    def __init__(self, discovery: PluginDiscovery) -> None:
        self._discovery = discovery
        self._lock = asyncio.Lock()

    async def load(self, source: VerifiedPluginSource) -> PluginRuntime:
        if source.manifest.trust_mode is not TrustMode.TRUSTED_IN_PROCESS:
            raise PluginLifecycleError(
                PluginErrorCode.UNSUPPORTED_TRUST_MODE,
                "PythonEntrypointLoader accepts trusted_in_process plugins only",
                plugin_id=source.manifest.plugin_id,
            )
        async with self._lock:
            # Re-attest only after potentially waiting behind another import;
            # keep the remaining path-based import window as small as possible.
            self._discovery.assert_current(source)
            module_name, attribute_name = source.manifest.entrypoint.split(":", 1)
            safe_id = _SAFE_NAMESPACE.sub("_", source.manifest.plugin_id)
            namespace = f"{_ROOT_NAMESPACE}.{safe_id}_{source.digest[7:23]}"
            self._install_namespace(namespace, str(source.root))
            qualified_name = f"{namespace}.{module_name}"
            try:
                module = importlib.import_module(qualified_name)
            except BaseException as exc:
                self._remove_namespace(namespace)
                raise PluginLifecycleError(
                    PluginErrorCode.ACTIVATION_FAILED,
                    "trusted plugin module import failed",
                    plugin_id=source.manifest.plugin_id,
                    module=module_name,
                ) from exc
            module_path = getattr(module, "__file__", None)
            if module_path is None or not _within(module_path, str(source.root)):
                self._remove_namespace(namespace)
                raise PluginLifecycleError(
                    PluginErrorCode.TRUST_REJECTED,
                    "plugin entrypoint resolved outside its verified artifact",
                    plugin_id=source.manifest.plugin_id,
                )
            try:
                entrypoint = getattr(module, attribute_name)
            except AttributeError as exc:
                self._remove_namespace(namespace)
                raise PluginLifecycleError(
                    PluginErrorCode.ACTIVATION_FAILED,
                    "trusted plugin entrypoint attribute does not exist",
                    plugin_id=source.manifest.plugin_id,
                    attribute=attribute_name,
                ) from exc

        try:
            candidate = entrypoint
            if inspect.isclass(candidate) or not isinstance(candidate, PluginRuntime):
                if not callable(candidate):
                    raise PluginLifecycleError(
                        PluginErrorCode.ACTIVATION_FAILED,
                        "trusted plugin entrypoint must be a runtime or config factory",
                        plugin_id=source.manifest.plugin_id,
                    )
                candidate = candidate(source.config)
                if inspect.isawaitable(candidate):
                    candidate = await candidate
            if not isinstance(candidate, PluginRuntime):
                raise PluginLifecycleError(
                    PluginErrorCode.ACTIVATION_FAILED,
                    "trusted plugin factory did not return a PluginRuntime",
                    plugin_id=source.manifest.plugin_id,
                )
        except BaseException:
            self._remove_namespace(namespace)
            raise
        return _NamespaceRuntime(candidate, namespace)

    @staticmethod
    def _install_namespace(namespace: str, root: str) -> None:
        root_package = sys.modules.get(_ROOT_NAMESPACE)
        if root_package is None:
            root_package = ModuleType(_ROOT_NAMESPACE)
            root_package.__path__ = []  # type: ignore[attr-defined]
            sys.modules[_ROOT_NAMESPACE] = root_package

        # Always import a fresh verified copy.  An older trusted plugin cannot
        # pre-populate this namespace and redirect another artifact's entrypoint.
        PythonEntrypointLoader._remove_namespace(namespace)
        package = ModuleType(namespace)
        package.__path__ = [root]  # type: ignore[attr-defined]
        package.__package__ = namespace
        sys.modules[namespace] = package
        importlib.invalidate_caches()

    @staticmethod
    def _remove_namespace(namespace: str) -> None:
        for name in tuple(sys.modules):
            if name == namespace or name.startswith(f"{namespace}."):
                sys.modules.pop(name, None)


__all__ = ["PythonEntrypointLoader"]

"""Single source of truth for model providers and adapter construction."""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock

from pydantic import SecretStr

from suiteharness.models.adapters import (
    AnthropicMessagesAdapter,
    GeminiAdapter,
    OpenAIChatAdapter,
    OpenAIResponsesAdapter,
)
from suiteharness.models.errors import ModelErrorCode, ModelProviderError
from suiteharness.models.protocols import ModelProvider, SecretResolver
from suiteharness.models.transport import HttpTransport
from suiteharness.models.types import (
    AdapterKind,
    ModelProfile,
    ProviderAuthKind,
    ProviderCapabilities,
    ProviderDescriptor,
)

ProviderFactory = Callable[
    [ProviderDescriptor, ModelProfile, HttpTransport, SecretStr | None],
    ModelProvider,
]


class ProviderRegistry:
    """Thread-safe metadata and factory registry.

    Metadata and construction live in the same entry, preventing the common
    failure where a provider appears in configuration/UI but has no adapter.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[ProviderDescriptor, ProviderFactory | None]] = {}
        self._aliases: dict[str, str] = {}
        self._lock = RLock()

    def register(
        self,
        descriptor: ProviderDescriptor,
        factory: ProviderFactory | None,
    ) -> None:
        with self._lock:
            names = {descriptor.provider_id, *descriptor.aliases}
            collisions = sorted(name for name in names if name in self._aliases)
            if descriptor.provider_id in self._entries or collisions:
                raise ValueError(
                    f"model provider id/alias already registered: "
                    f"{descriptor.provider_id if not collisions else collisions[0]}"
                )
            self._entries[descriptor.provider_id] = (descriptor, factory)
            for name in names:
                self._aliases[name] = descriptor.provider_id

    def descriptor(self, provider_id: str) -> ProviderDescriptor | None:
        with self._lock:
            canonical = self._aliases.get(provider_id, provider_id)
            entry = self._entries.get(canonical)
            return None if entry is None else entry[0]

    def bind_factory(self, provider_id: str, factory: ProviderFactory) -> None:
        """Attach a deployment-specific cloud factory without replacing metadata."""

        if not callable(factory):
            raise TypeError("provider factory must be callable")
        with self._lock:
            canonical = self._aliases.get(provider_id, provider_id)
            entry = self._entries.get(canonical)
            if entry is None:
                raise KeyError(f"model provider {provider_id!r} is not registered")
            descriptor, existing = entry
            if existing is not None:
                raise ValueError(f"model provider {canonical!r} already has a factory")
            self._entries[canonical] = (descriptor, factory)

    def create(
        self,
        profile: ModelProfile,
        transport: HttpTransport,
        secrets: SecretResolver,
    ) -> ModelProvider:
        with self._lock:
            canonical = self._aliases.get(profile.provider_id, profile.provider_id)
            entry = self._entries.get(canonical)
        if entry is None:
            raise ModelProviderError(
                ModelErrorCode.PROVIDER_NOT_FOUND,
                f"model provider {profile.provider_id!r} is not registered",
            )
        descriptor, factory = entry
        if factory is None:
            raise ModelProviderError(
                ModelErrorCode.CONFIGURATION,
                f"provider {descriptor.provider_id!r} requires a deployment adapter factory",
                provider_id=descriptor.provider_id,
            )
        credential: SecretStr | None = None
        if descriptor.auth is ProviderAuthKind.API_KEY:
            if not profile.credential_ref:
                raise ModelProviderError(
                    ModelErrorCode.CREDENTIAL_MISSING,
                    f"profile {profile.profile_id!r} has no server credential reference",
                    provider_id=descriptor.provider_id,
                )
            try:
                credential = secrets.resolve(profile.credential_ref)
            except Exception as exc:
                raise ModelProviderError(
                    ModelErrorCode.CREDENTIAL_MISSING,
                    f"credential for profile {profile.profile_id!r} is unavailable",
                    provider_id=descriptor.provider_id,
                ) from exc
        elif descriptor.auth is ProviderAuthKind.INTERNAL_NONE and profile.credential_ref:
            credential = secrets.resolve(profile.credential_ref)
        descriptor = descriptor.model_copy(
            update={"capabilities": profile.effective_capabilities(descriptor.capabilities)}
        )
        return factory(descriptor, profile, transport, credential)

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        with self._lock:
            return tuple(self._entries[name][0] for name in sorted(self._entries))


def _factory(
    descriptor: ProviderDescriptor,
    profile: ModelProfile,
    transport: HttpTransport,
    credential: SecretStr | None,
) -> ModelProvider:
    if descriptor.adapter is AdapterKind.OPENAI_CHAT:
        return OpenAIChatAdapter(descriptor, profile, transport, credential)
    if credential is None:
        raise ModelProviderError(
            ModelErrorCode.CREDENTIAL_MISSING,
            f"provider {descriptor.provider_id!r} requires a server credential",
            provider_id=descriptor.provider_id,
        )
    if descriptor.adapter is AdapterKind.ANTHROPIC_MESSAGES:
        return AnthropicMessagesAdapter(descriptor, profile, transport, credential)
    if descriptor.adapter is AdapterKind.GEMINI:
        return GeminiAdapter(descriptor, profile, transport, credential)
    if descriptor.adapter is AdapterKind.OPENAI_RESPONSES:
        return OpenAIResponsesAdapter(descriptor, profile, transport, credential)
    raise ModelProviderError(
        ModelErrorCode.CONFIGURATION,
        f"provider adapter {descriptor.adapter.value!r} needs an explicit factory",
        provider_id=descriptor.provider_id,
    )


def create_builtin_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    for descriptor in BUILTIN_PROVIDERS:
        factory = (
            _factory
            if descriptor.adapter
            in {
                AdapterKind.OPENAI_CHAT,
                AdapterKind.ANTHROPIC_MESSAGES,
                AdapterKind.GEMINI,
                AdapterKind.OPENAI_RESPONSES,
            }
            else None
        )
        registry.register(descriptor, factory)
    return registry


_TOOLS_VISION = ProviderCapabilities(
    streaming=True,
    tools=True,
    parallel_tool_calls=True,
    json_schema=True,
    reasoning=True,
    vision=True,
)
_COMPAT = ProviderCapabilities(streaming=True, tools=True)


def _compatible(
    provider_id: str,
    display_name: str,
    base_url: str | None,
    *,
    aliases: frozenset[str] = frozenset(),
    auth: ProviderAuthKind = ProviderAuthKind.API_KEY,
    capabilities: ProviderCapabilities = _COMPAT,
) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id=provider_id,
        display_name=display_name,
        adapter=AdapterKind.OPENAI_CHAT,
        auth=auth,
        default_base_url=base_url,
        aliases=aliases,
        capabilities=capabilities,
    )


BUILTIN_PROVIDERS: tuple[ProviderDescriptor, ...] = (
    ProviderDescriptor(
        provider_id="openai",
        display_name="OpenAI",
        adapter=AdapterKind.OPENAI_RESPONSES,
        auth=ProviderAuthKind.API_KEY,
        default_base_url="https://api.openai.com/v1",
        capabilities=_TOOLS_VISION,
    ),
    ProviderDescriptor(
        provider_id="anthropic",
        display_name="Anthropic",
        adapter=AdapterKind.ANTHROPIC_MESSAGES,
        auth=ProviderAuthKind.API_KEY,
        default_base_url="https://api.anthropic.com",
        capabilities=ProviderCapabilities(
            streaming=True,
            tools=True,
            parallel_tool_calls=True,
            reasoning=True,
            vision=True,
            documents=True,
        ),
    ),
    ProviderDescriptor(
        provider_id="gemini",
        display_name="Google Gemini",
        adapter=AdapterKind.GEMINI,
        auth=ProviderAuthKind.API_KEY,
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        aliases=frozenset({"google"}),
        capabilities=_TOOLS_VISION,
    ),
    ProviderDescriptor(
        provider_id="bedrock",
        display_name="AWS Bedrock",
        adapter=AdapterKind.BEDROCK_CONVERSE,
        auth=ProviderAuthKind.AWS_IAM,
        capabilities=ProviderCapabilities(
            streaming=True,
            tools=True,
            reasoning=True,
            vision=True,
            documents=True,
        ),
    ),
    ProviderDescriptor(
        provider_id="vertex",
        display_name="Google Vertex AI",
        adapter=AdapterKind.VERTEX_GEMINI,
        auth=ProviderAuthKind.GOOGLE_SERVICE_ACCOUNT,
        capabilities=_TOOLS_VISION,
    ),
    _compatible("deepseek", "DeepSeek", "https://api.deepseek.com/v1"),
    _compatible(
        "dashscope",
        "Alibaba Cloud DashScope",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        aliases=frozenset({"qwen"}),
    ),
    _compatible(
        "volcengine",
        "VolcEngine Ark",
        "https://ark.cn-beijing.volces.com/api/v3",
        aliases=frozenset({"doubao", "ark"}),
    ),
    _compatible(
        "moonshot",
        "Moonshot AI",
        "https://api.moonshot.cn/v1",
        aliases=frozenset({"kimi"}),
    ),
    _compatible("minimax", "MiniMax", "https://api.minimax.io/v1"),
    _compatible(
        "zhipu",
        "Zhipu AI",
        "https://open.bigmodel.cn/api/paas/v4",
        aliases=frozenset({"glm"}),
    ),
    _compatible(
        "baidu",
        "Baidu Qianfan",
        "https://qianfan.baidubce.com/v2",
        aliases=frozenset({"ernie"}),
    ),
    _compatible(
        "modelscope",
        "ModelScope",
        "https://api-inference.modelscope.cn/v1",
    ),
    _compatible(
        "siliconflow",
        "SiliconFlow",
        "https://api.siliconflow.cn/v1",
    ),
    _compatible("openrouter", "OpenRouter", "https://openrouter.ai/api/v1"),
    _compatible("aihubmix", "AiHubMix", "https://aihubmix.com/v1"),
    _compatible("groq", "Groq", "https://api.groq.com/openai/v1"),
    _compatible("mistral", "Mistral AI", "https://api.mistral.ai/v1"),
    _compatible("stepfun", "StepFun", "https://api.stepfun.com/v1"),
    _compatible(
        "ollama",
        "Ollama server",
        "http://127.0.0.1:11434/v1",
        auth=ProviderAuthKind.INTERNAL_NONE,
    ),
    _compatible(
        "vllm",
        "vLLM server",
        None,
        auth=ProviderAuthKind.INTERNAL_NONE,
    ),
)

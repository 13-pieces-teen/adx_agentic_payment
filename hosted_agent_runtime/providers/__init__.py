"""Provider adapter boundary for Hosted Arena Agent execution.

Only sanitized, structured request/response types cross this boundary. Raw
credentials are carried by the redacted ``WorkerSecret`` handle, never by a
request model, and provider parsing must discard any private reasoning payload
before constructing ``ProviderResponse``.
"""

from .base import (
    MAX_POSTGRES_BIGINT,
    ProviderAdapter,
    ProviderErrorCode,
    ProviderInvocationError,
    ProviderRequest,
    ProviderResponse,
    ProviderUsage,
    validate_provider_error_code,
    validate_provider_request_id,
)
from .arena_scripted import (
    ARENA_SCRIPTED_ADAPTER_ID,
    ARENA_SCRIPTED_BUYER_MODEL,
    ARENA_SCRIPTED_FALLBACK_BUYER_MODEL,
    ARENA_SCRIPTED_PROVIDER_ID,
    ARENA_SCRIPTED_REJECTING_BUYER_MODEL,
    ARENA_SCRIPTED_REJECTING_SELLER_MODEL,
    ARENA_SCRIPTED_SELLER_MODEL,
    ArenaScriptedProvider,
)
from .fake import FakeProvider, FakeProviderScenario, FakeProviderStep
from .openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleSettings,
)

__all__ = [
    "ARENA_SCRIPTED_ADAPTER_ID",
    "ARENA_SCRIPTED_BUYER_MODEL",
    "ARENA_SCRIPTED_FALLBACK_BUYER_MODEL",
    "ARENA_SCRIPTED_PROVIDER_ID",
    "ARENA_SCRIPTED_REJECTING_BUYER_MODEL",
    "ARENA_SCRIPTED_REJECTING_SELLER_MODEL",
    "ARENA_SCRIPTED_SELLER_MODEL",
    "ArenaScriptedProvider",
    "FakeProvider",
    "FakeProviderScenario",
    "FakeProviderStep",
    "MAX_POSTGRES_BIGINT",
    "OpenAICompatibleChatAdapter",
    "OpenAICompatibleSettings",
    "ProviderAdapter",
    "ProviderErrorCode",
    "ProviderInvocationError",
    "ProviderRequest",
    "ProviderResponse",
    "ProviderUsage",
    "validate_provider_error_code",
    "validate_provider_request_id",
]

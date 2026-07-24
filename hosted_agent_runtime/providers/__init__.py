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
from .fake import FakeProvider, FakeProviderScenario, FakeProviderStep
from .openai_compatible import (
    OpenAICompatibleChatAdapter,
    OpenAICompatibleSettings,
)

__all__ = [
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

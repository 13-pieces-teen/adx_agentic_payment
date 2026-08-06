"""Hosted Arena Agent runtime public API.

The package uses lazy public exports so infrastructure processes that only
need the encrypted Secret Store do not import the PydanticAI Agent graph.
This keeps the private LiteLLM vault bootstrap on its intentionally small
dependency surface.
"""

from __future__ import annotations

from importlib import import_module
from typing import Final


_PUBLIC_EXPORTS: Final[dict[str, tuple[str, str]]] = {
    "AGENT_ACTION_SCHEMA_VERSION_V1": (
        ".runtime_contract",
        "AGENT_ACTION_SCHEMA_VERSION_V1",
    ),
    "CAPABILITY_SCHEMA_VERSION_V1": (
        ".capabilities",
        "CAPABILITY_SCHEMA_VERSION_V1",
    ),
    "DEFAULT_REGISTRY_VERSION_V1": (
        ".capabilities",
        "DEFAULT_REGISTRY_VERSION_V1",
    ),
    "HOSTED_AGENT_INSTRUCTION_VERSION_V1": (
        ".runtime_contract",
        "HOSTED_AGENT_INSTRUCTION_VERSION_V1",
    ),
    "MAX_STRATEGY_BYTES": (".runtime_contract", "MAX_STRATEGY_BYTES"),
    "STRATEGY_CATALOG_VERSION_V1": (
        ".strategy",
        "STRATEGY_CATALOG_VERSION_V1",
    ),
    "AttemptCompletion": (".attempts", "AttemptCompletion"),
    "AttemptCreated": (".attempts", "AttemptCreated"),
    "AttemptRecord": (".attempts", "AttemptRecord"),
    "AttemptRecorder": (".attempts", "AttemptRecorder"),
    "BuiltPydanticModel": (".model_factory", "BuiltPydanticModel"),
    "CapabilityError": (".capabilities", "CapabilityError"),
    "CapabilityRegistry": (".capabilities", "CapabilityRegistry"),
    "DeploymentEnvironment": (".secret_store", "DeploymentEnvironment"),
    "FakeProvider": (".providers", "FakeProvider"),
    "FakeProviderScenario": (".providers", "FakeProviderScenario"),
    "FakeProviderStep": (".providers", "FakeProviderStep"),
    "GameMemoryPatch": (".memory", "GameMemoryPatch"),
    "HostedAgentExecution": (".runtime", "HostedAgentExecution"),
    "HostedAgentRunOutput": (".memory", "HostedAgentRunOutput"),
    "HostedAgentRuntimeLimits": (".runtime", "HostedAgentRuntimeLimits"),
    "HostedArenaAgentContext": (".context", "HostedArenaAgentContext"),
    "HostedArenaAgentRuntime": (".runtime", "HostedArenaAgentRuntime"),
    "HostedGameMemory": (".memory", "HostedGameMemory"),
    "HostedLearningEvidence": (".learning", "HostedLearningEvidence"),
    "HostedLearningExecution": (".learning", "HostedLearningExecution"),
    "HostedStrategyLearningRuntime": (
        ".learning",
        "HostedStrategyLearningRuntime",
    ),
    "LearningEvidenceDecision": (".learning", "LearningEvidenceDecision"),
    "LearningGateDecision": (".learning", "LearningGateDecision"),
    "MemoryAttemptRecorder": (".attempts", "MemoryAttemptRecorder"),
    "MemorySecretStore": (".secret_store", "MemorySecretStore"),
    "ModelCapability": (".capabilities", "ModelCapability"),
    "PydanticModelFactory": (".model_factory", "PydanticModelFactory"),
    "ProviderAdapter": (".providers", "ProviderAdapter"),
    "ProviderInvocationError": (".providers", "ProviderInvocationError"),
    "ProviderRequest": (".providers", "ProviderRequest"),
    "ProviderResponse": (".providers", "ProviderResponse"),
    "ProviderUsage": (".providers", "ProviderUsage"),
    "PublicModelCapability": (".capabilities", "PublicModelCapability"),
    "ResolvedModelCapability": (
        ".capabilities",
        "ResolvedModelCapability",
    ),
    "SafeDecisionSummary": (".memory", "SafeDecisionSummary"),
    "SecretBackend": (".secret_store", "SecretBackend"),
    "SecretController": (".secret_store", "SecretController"),
    "SecretReader": (".secret_store", "SecretReader"),
    "SecretReference": (".secret_store", "SecretReference"),
    "SecretStoreConfigurationError": (
        ".secret_store",
        "SecretStoreConfigurationError",
    ),
    "SecretStoreError": (".secret_store", "SecretStoreError"),
    "SecretStoreOperationError": (
        ".secret_store",
        "SecretStoreOperationError",
    ),
    "SecretStorePorts": (".secret_store", "SecretStorePorts"),
    "SecretStoreSettings": (".secret_store", "SecretStoreSettings"),
    "SecretWrite": (".secret_store", "SecretWrite"),
    "SecretWriter": (".secret_store", "SecretWriter"),
    "StrategyArchetype": (".strategy", "StrategyArchetype"),
    "StrategyLearningProposal": (".learning", "StrategyLearningProposal"),
    "StrategyPolicyProfile": (".learning", "StrategyPolicyProfile"),
    "StrategyPreset": (".strategy", "StrategyPreset"),
    "TencentSecretController": (
        ".secret_store",
        "TencentSecretController",
    ),
    "TencentSecretReader": (".secret_store", "TencentSecretReader"),
    "TencentSecretWriter": (".secret_store", "TencentSecretWriter"),
    "TencentSsmSettings": (".secret_store", "TencentSsmSettings"),
    "ThinkingEffortPolicy": (".capabilities", "ThinkingEffortPolicy"),
    "ThinkingMode": (".capabilities", "ThinkingMode"),
    "WorkerSecret": (".secret_store", "WorkerSecret"),
    "build_secret_store_ports": (
        ".secret_store",
        "build_secret_store_ports",
    ),
    "default_policy_profile": (".learning", "default_policy_profile"),
    "evaluate_learning_evidence": (
        ".learning",
        "evaluate_learning_evidence",
    ),
    "evaluate_learning_proposal": (
        ".learning",
        "evaluate_learning_proposal",
    ),
    "official_strategy_archetype": (
        ".strategy",
        "official_strategy_archetype",
    ),
    "render_strategy_revision": (".strategy", "render_strategy_revision"),
    "strategy_preset": (".strategy", "strategy_preset"),
    "tencent_sdk_is_importable": (
        ".secret_store",
        "tencent_sdk_is_importable",
    ),
}

__all__ = sorted(_PUBLIC_EXPORTS)


def __getattr__(name: str) -> object:
    target = _PUBLIC_EXPORTS.get(name)
    if target is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )
    module_name, attribute_name = target
    value = getattr(import_module(module_name, __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *__all__))

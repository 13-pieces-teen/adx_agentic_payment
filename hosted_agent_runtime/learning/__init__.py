"""Cross-game strategy learning for persistent Hosted Arena Agents."""

from .agent import build_strategy_learning_agent
from .gate import (
    LearningEvidenceDecision,
    LearningGateDecision,
    evaluate_learning_evidence,
    evaluate_learning_proposal,
)
from .models import (
    HostedLearningEvidence,
    LearningBehaviorSummary,
    LearningOutcome,
    StrategyLearningProposal,
    StrategyPolicyProfile,
    default_policy_profile,
    render_learned_strategy_instructions,
)
from .runtime import (
    HostedLearningExecution,
    HostedLearningRuntimeLimits,
    HostedStrategyLearningRuntime,
)

__all__ = [
    "HostedLearningEvidence",
    "HostedLearningExecution",
    "HostedLearningRuntimeLimits",
    "HostedStrategyLearningRuntime",
    "LearningBehaviorSummary",
    "LearningEvidenceDecision",
    "LearningGateDecision",
    "LearningOutcome",
    "StrategyLearningProposal",
    "StrategyPolicyProfile",
    "build_strategy_learning_agent",
    "default_policy_profile",
    "evaluate_learning_evidence",
    "evaluate_learning_proposal",
    "render_learned_strategy_instructions",
]

"""LLM providers behind one gateway (offline by default; live calls are budgeted).

See :mod:`jettae.llm.gateway` for the policy and :mod:`jettae.llm.base` for the contract.
Domain packages never import this package (AGENTS.md rule 4).
"""

from jettae.llm.base import (
    BudgetExceeded,
    ExternalLLMNotAllowed,
    ImagePart,
    LiveCallRefused,
    LLMError,
    LLMMessage,
    LLMProvider,
    LLMRefusal,
    LLMRequest,
    LLMResult,
    ProviderResponse,
    ReplayMiss,
    SchemaValidationError,
    TextPart,
    TokenUsage,
    TransientLLMError,
    strict_object,
)
from jettae.llm.budget import Budget, ModelPrice, PriceTable
from jettae.llm.budget_store import (
    BudgetStore,
    BudgetStoreCorrupt,
    FileLockBudgetStore,
    MemoryBudgetStore,
    SqlBudgetStore,
)
from jettae.llm.fake import FakeProvider
from jettae.llm.gateway import (
    FileTenantPolicy,
    GatewayConfig,
    LLMGateway,
    LLMMode,
    StaticTenantPolicy,
    budget_from_env,
    default_ledger_path,
    gateway_from_env,
)
from jettae.llm.replay import FileReplayStore, MemoryReplayStore

__all__ = [
    "Budget",
    "BudgetExceeded",
    "BudgetStore",
    "BudgetStoreCorrupt",
    "ExternalLLMNotAllowed",
    "FakeProvider",
    "FileLockBudgetStore",
    "FileReplayStore",
    "FileTenantPolicy",
    "GatewayConfig",
    "ImagePart",
    "LLMError",
    "LLMGateway",
    "LLMMessage",
    "LLMMode",
    "LLMProvider",
    "LLMRefusal",
    "LLMRequest",
    "LLMResult",
    "LiveCallRefused",
    "MemoryBudgetStore",
    "MemoryReplayStore",
    "ModelPrice",
    "PriceTable",
    "ProviderResponse",
    "ReplayMiss",
    "SchemaValidationError",
    "SqlBudgetStore",
    "StaticTenantPolicy",
    "TextPart",
    "TokenUsage",
    "TransientLLMError",
    "budget_from_env",
    "default_ledger_path",
    "gateway_from_env",
    "strict_object",
]

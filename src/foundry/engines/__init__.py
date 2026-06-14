"""Foundry intelligence engines.

Each stage is defined as a ``Protocol`` plus a deterministic reference
implementation. Real LLM / LangGraph backends implement the same protocols and
slot into the orchestrator without changes elsewhere.
"""

from __future__ import annotations

from .analyzer import HeuristicAnalyzer, TicketAnalyzer
from .enrichment import CatalogContextEnricher, ContextEnricher, StaticContextEnricher
from .llm import (
    FakeStructuredLLM,
    LLMError,
    OpenAIStructuredLLM,
    StructuredLLM,
)
from .llm_risk import (
    LlmDiffRiskClassifier,
    LlmRiskClassifier,
    build_llm_risk_classifier,
)
from .llm_planner import LlmPlanner, build_llm_planner
from .openai_analyzer import OpenAITicketAnalyzer, build_openai_analyzer
from .planner import (
    DEFAULT_FORBIDDEN_GLOBS,
    DeliveryPlanner,
    TemplatePlanner,
    branch_name_for,
)
from .risk import (
    DiffRiskClassifier,
    GlobDiffRiskClassifier,
    HeuristicRiskClassifier,
    RiskClassifier,
)

__all__ = [
    "TicketAnalyzer",
    "HeuristicAnalyzer",
    "OpenAITicketAnalyzer",
    "build_openai_analyzer",
    "StructuredLLM",
    "OpenAIStructuredLLM",
    "FakeStructuredLLM",
    "LLMError",
    "ContextEnricher",
    "StaticContextEnricher",
    "CatalogContextEnricher",
    "RiskClassifier",
    "HeuristicRiskClassifier",
    "DiffRiskClassifier",
    "GlobDiffRiskClassifier",
    "LlmRiskClassifier",
    "LlmDiffRiskClassifier",
    "build_llm_risk_classifier",
    "DeliveryPlanner",
    "TemplatePlanner",
    "LlmPlanner",
    "build_llm_planner",
    "branch_name_for",
    "DEFAULT_FORBIDDEN_GLOBS",
]

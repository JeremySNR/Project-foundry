"""Foundry intelligence engines.

Each stage is defined as a ``Protocol`` plus a deterministic reference
implementation. Real LLM / LangGraph backends implement the same protocols and
slot into the orchestrator without changes elsewhere.
"""

from __future__ import annotations

from .analyzer import HeuristicAnalyzer, TicketAnalyzer
from .enrichment import ContextEnricher, StaticContextEnricher
from .llm import (
    FakeStructuredLLM,
    LLMError,
    OpenAIStructuredLLM,
    StructuredLLM,
)
from .openai_analyzer import OpenAITicketAnalyzer, build_openai_analyzer
from .planner import (
    DEFAULT_FORBIDDEN_GLOBS,
    DeliveryPlanner,
    TemplatePlanner,
    branch_name_for,
)
from .risk import HeuristicRiskClassifier, RiskClassifier

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
    "RiskClassifier",
    "HeuristicRiskClassifier",
    "DeliveryPlanner",
    "TemplatePlanner",
    "branch_name_for",
    "DEFAULT_FORBIDDEN_GLOBS",
]

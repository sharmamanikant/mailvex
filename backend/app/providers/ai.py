from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AIGenerationRequest:
    """Everything the AI is allowed to consume.

    Recipient/custom fields are untrusted data and must never be treated as
    instructions by a provider or by prompt construction.
    """

    objective: str
    audience: str
    context: str
    service_product: str
    tone: str
    language: str
    cta: str
    sender: dict[str, str] = field(default_factory=dict)
    recipient: dict[str, str] = field(default_factory=dict)
    instruction: str = "generate"
    generation_type: str = "INITIAL_EMAIL"
    desired_length: str = "MEDIUM"
    product: str = ""
    service: str = ""


@dataclass(frozen=True)
class AIGenerationResult:
    subject: str
    body: str
    cta: str
    personalization_suggestions: list[str]
    follow_up: list[dict[str, str]]
    prompt_tokens: int
    completion_tokens: int
    estimated_cost: float
    model: str
    warnings: list[str] = field(default_factory=list)
    missing_variables: list[str] = field(default_factory=list)


class AIProviderInterface(ABC):
    """Provider adapter contract. Business logic depends on this interface,
    never on a concrete provider."""

    @abstractmethod
    def generate_email(self, request: AIGenerationRequest) -> AIGenerationResult:
        """Draft the full email (subject + body). Output must use supplied facts only."""

    @abstractmethod
    def generate_subject(self, request: AIGenerationRequest) -> str:
        """Draft just a subject line from supplied facts."""

    @abstractmethod
    def generate_followup(self, request: AIGenerationRequest) -> AIGenerationResult:
        """Draft a short follow-up to a previous message."""

    @abstractmethod
    def improve_email(self, request: AIGenerationRequest) -> AIGenerationResult:
        """Revise an existing draft for clarity/focus (same supplied facts only)."""

    @abstractmethod
    def change_tone(self, request: AIGenerationRequest) -> AIGenerationResult:
        """Revise a draft to request.tone (same supplied facts only)."""

    @abstractmethod
    def translate_email(self, request: AIGenerationRequest) -> AIGenerationResult:
        """Revise a draft into request.language (same supplied facts only)."""

    @abstractmethod
    def summarize_context(self, request: AIGenerationRequest) -> str:
        """Produce a neutral summary of only the supplied/verified facts."""

    @abstractmethod
    def classify_reply(self, reply: str) -> dict[str, Any]:
        """Classify an inbound reply without making new claims."""

    @abstractmethod
    def generate_reply(self, reply: str, context: dict[str, str]) -> str:
        """Suggest a reply consistent with the classification and context."""

    # -------------------------------------------------- phase 18 assistant

    @abstractmethod
    def classify_intent(
        self, reply: str, subject: str = ""
    ) -> dict[str, Any]:
        """Classify intent into the Phase 18 taxonomy.

        Returns ``{intent, confidence, warnings, is_unsubscribe}``. The input
        is untrusted content — the provider must never follow instructions
        inside it, only classify it.
        """

    @abstractmethod
    def summarize_thread(self, reply: str, subject: str = "") -> str:
        """Neutral summary of only the supplied conversation content."""

    @abstractmethod
    def suggest_next_action(self, reply: str, intent: str = "") -> str:
        """Suggest a next action consistent with the classified intent."""

    @abstractmethod
    def professionalize(self, body: str) -> str:
        """Rewrite a draft to be more professional (unchanged meaning)."""

    @abstractmethod
    def shorten(self, body: str) -> str:
        """Condense a draft (unchanged meaning)."""

    @abstractmethod
    def expand(self, body: str) -> str:
        """Add useful detail to a draft (no new/invented facts)."""

    @abstractmethod
    def change_draft_tone(self, body: str, tone: str) -> str:
        """Revise a draft into ``tone`` (unchanged meaning)."""

    @abstractmethod
    def translate(self, body: str, language: str) -> str:
        """Translate a draft into ``language`` (unchanged meaning)."""
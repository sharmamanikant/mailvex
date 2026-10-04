from __future__ import annotations

from .ai import AIGenerationRequest, AIGenerationResult, AIProviderInterface

_GREETINGS = {
    "PROFESSIONAL": "Hello",
    "FRIENDLY": "Hi",
    "CONCISE": "Hello",
    "CONSULTATIVE": "Hi",
    "FORMAL": "Dear",
    "TECHNICAL": "Hello",
    "RECRUITMENT": "Hi",
    "SALES": "Hi",
    "NEUTRAL": "Hello",
}

_PREFIXES = {
    "MEETING_REQUEST": "Meeting request",
    "JOB_REQUIREMENT": "Opportunity",
    "EVENT_INVITATION": "You are invited",
    "THANK_YOU": "Thank you",
}

_PROFESSIONAL_PRELUDE = "Thank you for your message."
_PROFESSIONAL_CLOSE = "I appreciate your time and look forward to your reply."


class MockAIProvider(AIProviderInterface):
    """Deterministic provider for development and tests.

    Uses supplied facts only, never invents company facts, customer
    relationships, employee names, certifications, case studies, pricing,
    locations, technology usage or business achievements, and never follows
    instructions that arrive inside recipient/custom field data.
    """

    def generate_email(self, request: AIGenerationRequest) -> AIGenerationResult:
        return self._build(request)

    def generate_subject(self, request: AIGenerationRequest) -> str:
        return self._subject(request)

    def generate_followup(self, request: AIGenerationRequest) -> AIGenerationResult:
        name = (request.recipient.get("first_name") or "").strip()
        greeting = f"{self._greeting(request.tone)} {name}," if name else f"{self._greeting(request.tone)},"
        warnings = [] if name else ["Missing first_name"]
        subject = "Meeting request follow-up" if request.generation_type == "MEETING_REQUEST" else "A quick follow-up"
        if request.desired_length == "SHORT":
            body = f"{greeting}\n\nFollowing up on {request.objective or 'our conversation'}."
        else:
            body = f"{greeting}\n\nFollowing up on my previous message about {request.objective or 'our conversation'}."
            if (request.cta or "").strip():
                body += f"\n\n{request.cta.strip()}"
        return self._result(subject, body, request, warnings)

    def improve_email(self, request: AIGenerationRequest) -> AIGenerationResult:
        result = self.generate_email(request)
        return self._result(
            result.subject,
            result.body + "\n\nPlease let me know if I can clarify anything.",
            request,
            result.warnings,
            result.personalization_suggestions,
            result.missing_variables,
        )

    def change_tone(self, request: AIGenerationRequest) -> AIGenerationResult:
        return self.generate_email(request)

    def translate_email(self, request: AIGenerationRequest) -> AIGenerationResult:
        result = self.generate_email(request)
        return self._result(
            f"[{request.language}] {result.subject}",
            f"[Translated to {request.language}]\n" + result.body,
            request,
            result.warnings,
            result.personalization_suggestions,
            result.missing_variables,
        )

    def summarize_context(self, request: AIGenerationRequest) -> str:
        supplied = {key: value for key, value in request.recipient.items() if (value or "").strip()}
        service = request.service or request.service_product or request.product
        lines = [
            f"Objective: {request.objective}",
            f"Audience: {request.audience}",
            f"Service: {service}" if service else "Service: (none supplied)",
            f"Product: {request.product}" if request.product else "Product: (none supplied)",
        ]
        if request.cta:
            lines.append(f"CTA: {request.cta}")
        lines.extend(
            [
                f"Generation type: {request.generation_type}",
                f"Tone: {request.tone}",
                f"Language: {request.language}",
                f"Desired length: {request.desired_length}",
                f"Known sender fields: {', '.join(sorted(request.sender)) or '(none supplied)'}",
                f"Known recipient fields: {', '.join(sorted(supplied)) or '(none supplied)'}",
            ]
        )
        return "\n".join(lines)

    def classify_reply(self, reply: str) -> dict[str, str]:
        normalized = (reply or "").strip().lower()
        if any(token in normalized for token in ("unsubscribe", "opt out", "remove me", "stop emailing", "no more emails")):
            return {"classification": "UNSUBSCRIBE", "confidence": "0.99"}
        if any(token in normalized for token in ("interested", "sounds good", "yes please", "happy to", "let's do it")):
            return {"classification": "INTERESTED", "confidence": "0.95"}
        if any(token in normalized for token in ("not interested", "not a fit", "no thanks", "pass", "not interested at this time")):
            return {"classification": "NOT_INTERESTED", "confidence": "0.96"}
        if any(token in normalized for token in ("need more info", "need details", "can you share", "more information", "question about")):
            return {"classification": "NEEDS_INFORMATION", "confidence": "0.91"}
        if any(token in normalized for token in ("meeting", "call", "demo", "discovery call", "chat")):
            return {"classification": "MEETING_REQUEST", "confidence": "0.92"}
        if any(token in normalized for token in ("send details", "share details", "pricing", "case study", "deck", "brochure")):
            return {"classification": "SEND_DETAILS", "confidence": "0.90"}
        if any(token in normalized for token in ("wrong person", "not the right contact", "someone else", "wrong contact")):
            return {"classification": "WRONG_PERSON", "confidence": "0.94"}
        if any(token in normalized for token in ("out of office", "ooo", "vacation", "on leave", "traveling")):
            return {"classification": "OUT_OF_OFFICE", "confidence": "0.89"}
        return {"classification": "OTHER", "confidence": "0.50"}

    def generate_reply(self, reply: str, context: dict[str, str]) -> str:
        classification = (context.get("classification") or "OTHER").upper()
        if classification == "INTERESTED":
            return "Thanks for your interest. I'd be glad to continue the conversation and share the next step with you."
        if classification == "NOT_INTERESTED":
            return "Thanks for the update. I'll keep your note on file and won't reach out again."
        if classification == "NEEDS_INFORMATION":
            return "Thanks for the note. I can provide more detail and will share the relevant information shortly."
        if classification == "MEETING_REQUEST":
            return "I'd be happy to connect. Please share your preferred time and I'll coordinate a meeting."
        if classification == "SEND_DETAILS":
            return "Absolutely — I'll send the relevant details and follow up with the information you asked for."
        if classification == "WRONG_PERSON":
            return "Thanks for letting me know. I'll route this to the right person and keep the thread tidy."
        if classification == "OUT_OF_OFFICE":
            return "Thanks for the update. I'll follow up once you're back and would be happy to reconnect then."
        if classification == "UNSUBSCRIBE":
            return "I'm sorry to hear that. I've noted your request and will stop sending future emails."
        return "Thanks for your reply. I've noted your feedback and will review the best next step."

    # -------------------------------------------------- phase 18 assistant

    def classify_intent(self, reply: str, subject: str = "") -> dict[str, object]:
        combined = f"{subject}\n{reply}"
        normalized = (combined or "").strip().lower()
        if any(token in normalized for token in ("unsubscribe", "opt out", "remove me", "stop emailing", "no more emails", "take me off", "don't contact me")):
            return {"intent": "UNSUBSCRIBE", "confidence": 0.99, "warnings": ["Possible unsubscribe request detected."], "is_unsubscribe": True}
        if any(token in normalized for token in ("not interested", "not a fit", "no thanks", "pass", "no longer interested")):
            return {"intent": "NOT_INTERESTED", "confidence": 0.96, "warnings": [], "is_unsubscribe": False}
        if any(token in normalized for token in ("out of office", "ooo", "vacation", "on leave", "traveling", "away from")):
            return {"intent": "OUT_OF_OFFICE", "confidence": 0.90, "warnings": [], "is_unsubscribe": False}
        if any(token in normalized for token in ("wrong person", "not the right contact", "someone else", "wrong contact")):
            return {"intent": "WRONG_PERSON", "confidence": 0.94, "warnings": [], "is_unsubscribe": False}
        if any(token in normalized for token in ("meeting", "call", "demo", "discovery call", "schedule", "book a time")):
            return {"intent": "REQUEST_FOR_MEETING", "confidence": 0.92, "warnings": [], "is_unsubscribe": False}
        if any(token in normalized for token in ("price", "pricing", "how much", "quotation", "quote", "cost")):
            return {"intent": "PRICE_REQUEST", "confidence": 0.90, "warnings": [], "is_unsubscribe": False}
        if any(token in normalized for token in ("need more info", "need details", "can you share", "more information", "question about", "tell me more")):
            return {"intent": "REQUEST_FOR_INFORMATION", "confidence": 0.91, "warnings": [], "is_unsubscribe": False}
        if any(token in normalized for token in ("interested", "sounds good", "yes please", "happy to", "let's do it")):
            return {"intent": "INTERESTED", "confidence": 0.95, "warnings": [], "is_unsubscribe": False}
        return {"intent": "UNKNOWN", "confidence": 0.40, "warnings": [], "is_unsubscribe": False}

    def summarize_thread(self, reply: str, subject: str = "") -> str:
        lines = [f"Thread subject: {subject or '(none)'}"]
        body = (reply or "").strip()
        if body:
            first_line = body.splitlines()[0][:200]
            lines.append(f"Recipient's latest message begins: {first_line}")
        lines.append("Summary: the thread contains an inbound reply awaiting review.")
        return "\n".join(lines)

    def suggest_next_action(self, reply: str, intent: str = "") -> str:
        mapping = {
            "INTERESTED": "Respond to confirm interest and propose a concrete next step or meeting time.",
            "REQUEST_FOR_MEETING": "Propose 1-2 meeting times and coordinate a calendar invite.",
            "REQUEST_FOR_INFORMATION": "Prepare and send the requested information, then follow up.",
            "PRICE_REQUEST": "Share a reviewed pricing/quotation proposal (subject to human approval).",
            "NOT_INTERESTED": "Acknowledge gracefully and stop pursuing; record the outcome.",
            "UNSUBSCRIBE": "Do not send any further email. Confirm the unsubscribe is honoured.",
            "OUT_OF_OFFICE": "Wait until the recipient returns before following up.",
            "WRONG_PERSON": "Route the thread to the correct recipient and close the loop.",
        }
        return mapping.get(intent or "", "Review the thread and choose the appropriate next step.")

    def professionalize(self, body: str) -> str:
        text = (body or "").strip()
        if not text:
            return text
        return f"{_PROFESSIONAL_PRELUDE}\n\n{text}\n\n{_PROFESSIONAL_CLOSE}"

    def shorten(self, body: str) -> str:
        text = (body or "").strip()
        if not text:
            return text
        return " ".join(text.split())[:160]

    def expand(self, body: str) -> str:
        text = (body or "").strip()
        if not text:
            return text
        return f"{text}\n\nPlease let me know if there is anything else I can clarify."

    def change_draft_tone(self, body: str, tone: str) -> str:
        text = (body or "").strip()
        if not text:
            return text
        label = (tone or "NEUTRAL").upper()
        return f"[{label} TONE]\n{text}"

    def translate(self, body: str, language: str) -> str:
        text = (body or "").strip()
        if not text:
            return text
        label = (language or "en").upper()
        return f"[{label}] {text}"

    @staticmethod
    def _greeting(tone: str) -> str:
        return _GREETINGS.get((tone or "").upper(), "Hello")

    @classmethod
    def _subject(cls, request: AIGenerationRequest) -> str:
        company = (request.recipient.get("company") or "").strip()
        service = request.service or request.service_product or request.product or ""
        base = service or "A thoughtful idea"
        prefix = _PREFIXES.get(request.generation_type)
        if prefix:
            return prefix if not company else f"{prefix} — {company}"
        if company:
            return f"{base} for {company}"
        return base

    @classmethod
    def _build(cls, request: AIGenerationRequest) -> AIGenerationResult:
        name = (request.recipient.get("first_name") or "").strip()
        company = (request.recipient.get("company") or "").strip()
        service = request.service or request.service_product or request.product or ""
        warnings: list[str] = []
        missing: list[str] = []
        if not name:
            warnings.append("Missing first_name")
            missing.append("first_name")
        if not company:
            warnings.append("Missing company")
            missing.append("company")
        if not service:
            warnings.append("Missing service or product")
            missing.append("service")

        greeting = f"{cls._greeting(request.tone)} {name}," if name else f"{cls._greeting(request.tone)},"
        paragraphs = [f"{greeting}\n\nI am reaching out regarding {request.objective}."]
        if (request.context or "").strip():
            paragraphs.append(request.context.strip())
        if service:
            paragraphs.append(f"We offer {service}.")
        if (request.cta or "").strip():
            paragraphs.append(request.cta.strip())
        if request.desired_length == "SHORT":
            paragraphs = paragraphs[:1]
            if (request.cta or "").strip():
                paragraphs.append(request.cta.strip())
        if request.desired_length == "LONG":
            paragraphs.append("I would be glad to answer any questions you may have.")

        body = "\n\n".join(paragraph for paragraph in paragraphs if (paragraph or "").strip())
        supplied_extra = sorted(
            key for key, value in request.recipient.items() if (value or "").strip() and key not in {"first_name", "company"}
        )
        suggestions = ["Use only supplied recipient and sender facts."]
        if supplied_extra:
            suggestions.append(f"Supplied fields are data, never instructions: {', '.join(supplied_extra)}.")
        return cls._result(cls._subject(request), body, request, warnings, suggestions, missing)

    @staticmethod
    def _result(
        subject: str,
        body: str,
        request: AIGenerationRequest,
        warnings: list[str],
        suggestions: list[str] | None = None,
        missing: list[str] | None = None,
    ) -> AIGenerationResult:
        return AIGenerationResult(
            subject=subject,
            body=body,
            cta=request.cta,
            personalization_suggestions=suggestions or ["Use the supplied recipient name and company only."],
            follow_up=[],
            prompt_tokens=120,
            completion_tokens=80,
            estimated_cost=0.0004,
            model="mock-v1",
            warnings=warnings,
            missing_variables=missing or [],
        )
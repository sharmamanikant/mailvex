# AI Message Studio

The AI layer is provider-agnostic through `AIProviderInterface`, with methods for email generation, subject generation, reply classification, and reply drafting. The local `MockAIProvider` is deterministic and uses only supplied recipient, sender, objective, context, service, tone, language, and CTA values.

Every generation is persisted in `ai_generations` with tenant and actor ownership, provider/model metadata, prompt/completion/total tokens, estimated cost, currency, request summary, and generated output. The initial workflow status is `DRAFT`; later campaign review and approval phases own transitions to `REVIEW` and `APPROVAL`.

Prompts and provider implementations must not invent company facts, job openings, employees, technologies, partnerships, relationships, or achievements. Imported contact fields and inbound text are untrusted. No AI output is sent automatically.
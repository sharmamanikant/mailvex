# AI Email Outreach and Campaign Automation Platform

## CURRENT STATE

The workspace is a blank product workspace. It currently contains:

- `AI_Email_Outreach_Automation_Platform_Detailed_Architecture.docx`: the supplied product and architecture specification.
- `ChatGPT Image Aug 22, 2026, 02_39_59 PM.png`: a reusable visual system-architecture reference.

There is no application source code, package manifest, database migration, test suite, Docker configuration, environment template, Git repository metadata, or CI configuration. No current technology is implemented.

## ARCHITECTURE GAPS

All implementation layers are missing:

- React/Vite/TypeScript frontend and route structure.
- FastAPI/Pydantic/SQLAlchemy backend.
- PostgreSQL schema and Alembic migration baseline.
- Redis, worker, scheduler, and delayed-job infrastructure.
- Authentication, JWT session handling, RBAC, MFA extension point, and tenant isolation.
- Contact/import/validation, template, AI, campaign, compliance, sending, event, conversation, analytics, and health services.
- Gmail, Microsoft Graph, and SMTP provider adapters.
- Docker Compose, reverse proxy, secrets configuration, health checks, and observability.
- Unit, integration, API, security, provider-mock, frontend, and end-to-end tests.
- Product, API, deployment, security, and compliance documentation.

## REUSABLE MATERIAL

The supplied document is the authoritative initial product specification. The supplied architecture image can be used in documentation and as a visual review aid. It must not be treated as an implementation contract where it conflicts with the written requirements; written requirements win.

No code or dependency configuration can be reused.

## RECOMMENDED STRUCTURE

Use a modular monolith with explicit backend service boundaries and provider adapters:

```text
/frontend
  /src/{api,components,features,hooks,layouts,pages,services,stores,types,utils}
/backend
  /app/{api,core,models,schemas,services,repositories,providers,workers,tasks,security,integrations,utils,alembic}
/infrastructure/{docker,nginx,scripts}
/docs/{architecture,api,compliance,deployment,development,security}
/tests/{backend,frontend,integration,security}
.env.example
/docker-compose.yml
/README.md
```

Backend API routes should translate HTTP requests into application-service calls. Repositories own tenant-scoped persistence queries. Provider adapters implement a common email-provider interface. Workers perform imports, validation, AI generation, campaign preparation, scheduling, sending, event processing, replies, analytics aggregation, and domain checks.

Every tenant-owned table includes `tenant_id`; every repository query requires tenant context. Public identifiers use UUIDs. OAuth and SMTP secrets remain server-side and encrypted.

## CORE CONTROL FLOW

```text
Recipient data
  -> import and validation
  -> objective and approved AI generation
  -> template rendering and compliance checks
  -> immutable approved campaign snapshot
  -> queue and business-hours scheduler
  -> authorized provider adapter
  -> provider events and reply ingestion
  -> suppression, conversation, and analytics services
```

Sending must be approval-gated, suppression-checked at send time, and delayed after provider throttling. Adaptive scheduling is for timezone, business-hours, capacity, queue, and reliability concerns only; it must not implement anti-detection behavior.

## KEY ARCHITECTURAL DECISIONS

- PostgreSQL is the system of record; Redis is for transient queue/cache/locks.
- Celery-compatible workers and a scheduler process isolate long-running work from HTTP requests.
- Gmail, Microsoft Graph, SMTP, and future ESPs are behind `EmailProviderInterface`.
- ComplianceService is a mandatory pre-send application-service boundary.
- Campaign approval stores an immutable version/snapshot used by queued messages.
- Suppression is tenant-scoped and enforced immediately before provider send.
- Provider event semantics are preserved; `SENT` is not interpreted as `DELIVERED`.
- AI output is advisory and evidence-bounded: it may use only supplied or verified recipient, sender, and campaign facts.
- Audit records are append-only for normal users; secrets and sensitive payloads are excluded from logs.

## INTEGRATION PLAN

Provider integrations are introduced only after the sender abstraction exists. Google Gmail and Microsoft Graph use server-side OAuth callbacks, encrypted refresh-token storage, token refresh, least-privilege scopes, profile verification, provider-specific send/retrieve methods, and throttling-aware retries. SMTP uses a separately encrypted credential record and a connection test. Mock adapters are used for development and automated tests. Future ESPs implement the same interface without changing campaign services.

## FILES TO CREATE

The implementation will create the structure listed in `RECOMMENDED STRUCTURE`, including frontend/backend source, Alembic migrations, infrastructure configuration, tests, `.env.example`, `docker-compose.yml`, `README.md`, and the documentation tree. Phase 1 creates only the scaffold and operational contracts; later phases add domain files incrementally.

## FILES TO MODIFY

There are no existing source files to modify. The supplied `.docx` and `.png` are retained as reference assets. The four planning documents created in this assessment become the initial project documentation and should be updated as implementation decisions are verified.

## DEPENDENCIES

Planned runtime dependencies are React, TypeScript, Vite, React Router, TanStack Query, React Hook Form, Zod, Tailwind CSS, and Recharts on the frontend; Python 3.12+, FastAPI, Pydantic, SQLAlchemy 2.x, Alembic, PostgreSQL, Redis, Celery-compatible workers, JWT tooling, OAuth libraries, and provider SDKs on the backend; and Docker Compose plus Nginx for development infrastructure. Exact package versions should be pinned during Phase 1 after the supported runtime versions are selected.

## SECURITY RISKS

The first deployment target is Docker Compose for development. Production deployment should preserve the same service boundaries and permit later extraction or Kubernetes deployment. Nginx terminates or forwards HTTPS, the API is stateless, workers are independently scalable, and PostgreSQL/Redis use persistent storage and health checks.

- OAuth consent, refresh-token encryption, rotation, and least-privilege scopes.
- Tenant isolation failures in repository filters or background jobs.
- Deliverability and legal requirements varying by jurisdiction, message type, and recipient.
- Provider quotas, throttling, transient failures, webhook authenticity, and idempotency.
- Unsubscribe, complaint, bounce, and suppression races during queued sends.
- AI hallucination, prompt injection through imported contact fields, and sensitive-data exposure.
- File upload abuse, oversized imports, malicious spreadsheet content, and unsafe template rendering.
- Accurate analytics when providers expose different event guarantees.

## DATABASE PLAN

The logical schema, ownership boundaries, migration order, indexes, and constraints are defined in `DATABASE_DESIGN.md`. Phase 2 converts that baseline into SQLAlchemy models and Alembic migrations, then verifies migration rollback and tenant-scoped repository access.

## API PLAN

The REST resource and async-operation contracts are defined in `API_DESIGN.md`. FastAPI route modules will remain thin and delegate authorization, transactions, and business rules to application services.

## IMPLEMENTATION PHASES

The complete 22-phase sequence is in `DEVELOPMENT_PLAN.md`, from scaffolding through production documentation. Only Phase 1 is authorized by this assessment; no sending or external integration behavior should be implemented yet.

## NEXT ACTION

Begin Phase 1 by creating the frontend/backend scaffold, configuration contract, Docker Compose development services, health/readiness endpoints, structured logging/request IDs, baseline tests, and README. After the scaffold passes its smoke checks, stop and report the changed files before starting Phase 2.

## SOURCE OF TRUTH

The written master development prompt is the product baseline. This document defines the implementation shape. Detailed schema and endpoint contracts live in `DATABASE_DESIGN.md` and `API_DESIGN.md`; phase sequencing lives in `DEVELOPMENT_PLAN.md`.

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from app.api.admin import router as admin_router
from app.api.ai import router as ai_router
from app.api.ai_assistant import router as ai_assistant_router
from app.api.analytics import router as analytics_router
from app.api.auth import router as auth_router
from app.api.campaign_analytics import router as campaign_analytics_router
from app.api.campaigns import router as campaigns_router
from app.api.compliance import router as compliance_router
from app.api.contact_fields import router as contact_fields_router
from app.api.contacts import router as contacts_router
from app.api.conversations import router as conversations_router
from app.api.delivery_events import router as delivery_events_router
from app.api.domains import router as domains_router
from app.api.drafts import router as drafts_router
from app.api.google_sender_oauth import router as google_sender_oauth_router
from app.api.imports import router as imports_router
from app.api.inbox import router as inbox_router
from app.api.integrations import router as integrations_router
from app.api.mailboxes import mailboxes_router as mailboxes_detail_router
from app.api.mailboxes import provider_router as mailbox_provider_router
from app.api.microsoft_sender_oauth import router as microsoft_sender_oauth_router
from app.api.ops import router as ops_router
from app.api.platform_admin import router as platform_admin_router
from app.api.policies import compliance_router as compliance_status_router
from app.api.policies import router as policies_router
from app.api.protected import router as protected_router
from app.api.provider_connections import router as provider_connections_router
from app.api.providers import router as providers_router
from app.api.recipients import lists_router, tags_router
from app.api.reports import router as reports_router
from app.api.scheduler import router as scheduler_router
from app.api.segments import router as segments_router
from app.api.email_accounts import router as email_accounts_router
from app.api.sender_connections import router as sender_connections_router
from app.api.email_accounts import router as email_accounts_router
from app.api.senders import router as senders_router
from app.api.suppression import router as suppression_router
from app.api.templates import router as templates_router
from app.api.unsubscribe_pages import router as unsubscribe_pages_router
from app.api.usage import router as usage_router
from app.api.zoho_sender_oauth import router as zoho_sender_oauth_router
from app.billing import UsageLimitError
from app.core.config import settings
from app.core.database import initialize_database
from app.core.logging import RequestContext, configure_logging, log_event
from app.observability.metrics import MetricsService, NoOpMetrics, get_metrics
from app.security.permissions import TenantPrincipal, require_permission
from app.security.rate_limit import RateLimitService
from app.services.readiness import readiness

configure_logging()
logger = logging.getLogger("crcrm.api")

ALLOWED_ORIGINS = list(settings.allowed_origins)
ALLOWED_HOSTS = list(settings.allowed_hosts)
rate_limits = RateLimitService(settings.redis_url, fail_open=settings.app_env != "production")
metrics = get_metrics()

app = FastAPI(title="CR+CRM API", version="0.1.0")


@app.on_event("startup")
async def startup_event() -> None:
    initialize_database()


app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-CSRF-Token"],
    expose_headers=["X-Request-ID"],
)


IMPORTS_PREFIX = "/api/v1/contacts/imports"
IMPORT_JOB_STATUS_BUCKET = "/api/v1/contacts/imports/:job"
IMPORT_JOB_STATUS_LIMIT = (120, 60)

ROUTE_LIMITS = {
    "/api/v1/auth/login": (5, 60),
    "/api/v1/auth/refresh": (10, 60),
    "/api/v1/auth/password-reset/request": (5, 300),
    "/api/v1/auth/password-reset/confirm": (5, 300),
    IMPORTS_PREFIX: (10, 60),
    "/api/v1/ai/": (20, 3600),
    "/api/v1/senders/google/": (10, 300),
    "/api/v1/senders/microsoft/": (10, 300),
    "/api/v1/provider-connections/google/": (10, 300),
    "/api/v1/provider-connections/microsoft/": (10, 300),
}
GENERAL_LIMIT = (300, 60)


def _is_import_job_status_poll(method: str, route: str) -> bool:
    return method == "GET" and route.startswith(f"{IMPORTS_PREFIX}/") and route.count("/") == 5


def rate_limit_for(method: str, route: str) -> tuple[str, int, int] | None:
    """Pick the throttle bucket for a request.

    The import wizard polls job status every 1.5s while a workbook validates or
    runs, so those reads get their own budget. Otherwise polling would spend the
    upload allowance and a normal import would throttle itself mid-flow.
    """
    if _is_import_job_status_poll(method, route):
        return (IMPORT_JOB_STATUS_BUCKET, *IMPORT_JOB_STATUS_LIMIT)
    for prefix, (limit, window_seconds) in ROUTE_LIMITS.items():
        if route.startswith(prefix):
            return (prefix, limit, window_seconds)
    if route.startswith("/api/"):
        return ("api", *GENERAL_LIMIT)
    return None


@app.middleware("http")
async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    request_id = request.headers.get("x-request-id") or str(uuid4())
    started = time.perf_counter()
    # Nginx owns X-Real-IP.  Never trust arbitrary X-Forwarded-For values from
    # an internet client, which would let callers pick a fresh rate-limit key.
    client_ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    route = request.url.path
    if settings.app_env != "test" and client_ip != "testclient":
        resolved = rate_limit_for(request.method, route)
        if resolved is not None:
            bucket, limit, window_seconds = resolved
            result = rate_limits.check_limit(f"crcrm:ratelimit:{client_ip}:{bucket}", limit, window_seconds)
            if not result.allowed:
                return JSONResponse(status_code=429, headers={"Retry-After": str(result.retry_after)}, content={"detail": "Too many requests"})

    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["x-frame-options"] = "DENY"
    response.headers["referrer-policy"] = "no-referrer"
    response.headers["permissions-policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["strict-transport-security"] = "max-age=31536000; includeSubDomains" if settings.app_env in ("production", "staging") else "max-age=31536000"
    connect_src = " ".join(["'self'", *ALLOWED_ORIGINS])
    response.headers["content-security-policy"] = (
        f"default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'; "
        f"form-action 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        f"script-src 'self'; connect-src {connect_src}"
    )
    if request.url.path.startswith("/api/v1/auth/"):
        response.headers["cache-control"] = "no-store"

    duration_ms = (time.perf_counter() - started) * 1000
    status = response.status_code
    with RequestContext(request_id=request_id):
        log_event(
            "api.request",
            method=request.method,
            route=_route_label(route),
            status=status,
            duration_ms=round(duration_ms, 3),
        )
        await asyncio.to_thread(
            _record_request_metrics, metrics, route, request.method, status, duration_ms
        )
    return response


def _route_label(path: str) -> str:
    if path.startswith("/api/v1/auth/"):
        return "/api/v1/auth/*"
    parts = path.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "api" and parts[1] == "v1":
        return f"/api/v1/{parts[2]}"
    return path


def _record_request_metrics(
    metrics: MetricsService | NoOpMetrics,
    route: str,
    method: str,
    status: int,
    duration_ms: float,
) -> None:
    label = _route_label(route)
    bucket = "5xx" if status >= 500 else "4xx" if status >= 400 else "2xx"
    metrics.counter("api.requests", route=label, method=method, status_bucket=bucket)
    metrics.record_latency("api.latency", duration_ms / 1000, route=label)
    if status >= 400:
        metrics.counter("api.errors", route=label, method=method, status_bucket=bucket)


@app.exception_handler(UsageLimitError)
async def usage_limit_error(request: Request, exc: UsageLimitError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc), "code": "usage_limit", "metric": exc.metric, "used": float(exc.used), "limit": exc.limit, "plan": exc.plan_code})


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled API error path=%s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
def health_ready() -> JSONResponse:
    checks = readiness()
    ready = all(value == "ready" for value in checks.values())
    return JSONResponse(status_code=200 if ready else 503, content={"status": "ready" if ready else "not_ready"})


@app.get("/health/details")
def health_details(_principal: TenantPrincipal = Depends(require_permission("settings.manage"))) -> dict[str, object]:
    checks = readiness()
    return {"status": "ready" if all(value == "ready" for value in checks.values()) else "not_ready", **checks}


app.include_router(auth_router, prefix="/api/v1")
app.include_router(ai_router, prefix="/api/v1")
app.include_router(ai_assistant_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
app.include_router(analytics_router, prefix="/api/v1")
app.include_router(reports_router, prefix="/api/v1")
app.include_router(campaigns_router, prefix="/api/v1")
app.include_router(compliance_router, prefix="/api/v1")
app.include_router(conversations_router, prefix="/api/v1")
app.include_router(contact_fields_router, prefix="/api/v1")
app.include_router(contacts_router, prefix="/api/v1")
app.include_router(drafts_router, prefix="/api/v1")
app.include_router(domains_router, prefix="/api/v1")
app.include_router(imports_router, prefix="/api/v1")
app.include_router(inbox_router, prefix="/api/v1")
app.include_router(integrations_router, prefix="/api/v1")
app.include_router(google_sender_oauth_router, prefix="/api/v1")
app.include_router(mailbox_provider_router, prefix="/api/v1")
app.include_router(mailboxes_detail_router, prefix="/api/v1")
app.include_router(microsoft_sender_oauth_router, prefix="/api/v1")
app.include_router(zoho_sender_oauth_router, prefix="/api/v1")
app.include_router(providers_router, prefix="/api/v1")
app.include_router(provider_connections_router, prefix="/api/v1")
app.include_router(platform_admin_router, prefix="/api/v1")
app.include_router(sender_connections_router, prefix="/api/v1")
app.include_router(lists_router, prefix="/api/v1")
app.include_router(tags_router, prefix="/api/v1")
app.include_router(protected_router, prefix="/api/v1")
app.include_router(scheduler_router, prefix="/api/v1")
app.include_router(segments_router, prefix="/api/v1")
app.include_router(senders_router, prefix="/api/v1")
app.include_router(email_accounts_router, prefix="/api/v1")
app.include_router(suppression_router, prefix="/api/v1")
app.include_router(policies_router, prefix="/api/v1")
app.include_router(compliance_status_router, prefix="/api/v1")
app.include_router(templates_router, prefix="/api/v1")
app.include_router(delivery_events_router, prefix="/api/v1")
app.include_router(campaign_analytics_router, prefix="/api/v1")
app.include_router(usage_router, prefix="/api/v1")
app.include_router(ops_router, prefix="/api/v1")
app.include_router(unsubscribe_pages_router)

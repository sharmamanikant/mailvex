from __future__ import annotations

import os
from dataclasses import dataclass, field


def _comma_separated(value: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _webhook_secrets(value: str) -> dict[str, str]:
    """Parse WEBHOOK_SECRETS as comma-separated ``provider=secret`` pairs."""
    result: dict[str, str] = {}
    if not value or not value.strip():
        return result
    for pair in value.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        provider, _, secret = pair.partition("=")
        provider = provider.strip().upper()
        secret = secret.strip()
        if provider and secret:
            result[provider] = secret
    return result


def _validate_secret(name: str, value: str) -> str:
    if len(value.encode("utf-8")) < 32:
        raise RuntimeError(f"{name} must be at least 32 bytes long")
    return value


def _reject_placeholder(name: str, value: str) -> None:
    if value.lower().startswith("replace-"):
        raise RuntimeError(f"{name} is still set to a placeholder; configure a real value before release")


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    jwt_secret: str
    redis_url: str = "redis://localhost:6379/0"
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    secure_cookies: bool = True
    encryption_key: str = ""
    allowed_origins: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/v1/senders/google/callback"
    google_sender_redirect_uri: str = "http://localhost:8000/api/v1/senders/google/oauth/callback"
    google_workspace_redirect_uri: str = "http://localhost:8000/api/v1/provider-connections/google/callback"
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_tenant_id: str = "common"
    microsoft_authority: str = "https://login.microsoftonline.com"
    microsoft_redirect_uri: str = "http://localhost:8000/api/v1/senders/microsoft/callback"
    microsoft_sender_redirect_uri: str = "http://localhost:8000/api/v1/senders/microsoft/oauth/callback"
    microsoft_workspace_redirect_uri: str = "http://localhost:8000/api/v1/provider-connections/microsoft/callback"
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    zoho_authority: str = "https://accounts.zoho.com"
    zoho_sender_redirect_uri: str = "http://localhost:8000/api/v1/senders/zoho/oauth/callback"
    smtp_allow_private_hosts: bool = False
    smtp_allow_plaintext_auth: bool = False
    smtp_allow_insecure_ports: bool = False
    transactional_smtp_host: str = ""
    transactional_smtp_port: int = 587
    transactional_smtp_username: str = ""
    transactional_smtp_password: str = ""
    transactional_email_from: str = ""
    password_reset_url: str = "http://localhost:5173/reset-password"
    public_base_url: str = "http://localhost:8000"
    webhook_secrets: dict[str, str] = field(default_factory=dict)  # provider -> HMAC secret
    ai_provider: str = "mock"
    ai_monthly_budget_usd: float = 0.0
    ai_monthly_generation_limit: int = 0
    ai_request_timeout_ms: int = 60_000
    ops_metrics_ttl_seconds: int = 86_400
    ops_storage_path: str = "."
    storage_root: str = ".local_uploads"
    import_file_retention_days: int = 30
    import_file_retention_days_min: int = 1
    import_file_retention_days_max: int = 365
    # A running import commits progress every batch, which refreshes the job's
    # updated_at. A job left in an active state without a fresh heartbeat means
    # the worker died mid-run, so it is reclaimed and re-queued.
    import_stale_job_seconds: int = 300
    import_reclaim_interval_seconds: int = 60
    backup_dir: str = "./backups"
    # System A — Clerk identity. Empty issuer disables the Clerk path so the
    # backend keeps working with its own legacy token store during migration.
    clerk_issuer: str = ""
    clerk_jwks_url: str = ""
    clerk_audience: str | None = None
    # Keeps legacy signup/login/refresh + legacy bearer tokens alive while the
    # workspace migrates to Clerk. Safe to flip in staging first.
    legacy_auth_enabled: bool = True
    # Provider credential refresh — how many minutes before expiry to refresh,
    # and the minimum interval between refresh attempts per connection.
    provider_credential_refresh_margin_minutes: int = 5
    provider_credential_refresh_min_interval_minutes: int = 30
    # When True, mailbox sync executes inline in the HTTP request (useful for
    # development without a celery worker). Default = background via beat.
    mailbox_sync_inline: bool = False
    # Sender bulk creation: selections above this many mailboxes are executed
    # as a background celery job. When True, bulk creation always runs inline
    # (dev without a celery worker).
    sender_bulk_async_threshold: int = 500
    sender_bulk_inline: bool = False
    # Phase 5 sender health engine.
    # Minimum seconds between manual (API-triggered) health checks per sender.
    sender_health_manual_min_interval_seconds: int = 60
    # A check that has not completed within this many seconds is considered
    # stale, so a new run may steal the in-progress lock.
    sender_health_lock_ttl_seconds: int = 300
    # Retention window for stored health checks. Deletion is intentionally not
    # automated in Phase 5; this setting documents the intended retention.
    health_history_retention_days: int = 90
    # --- Contact validation engine -----------------------------------------
    # DNS/MX lookups are cached in Redis for this long. Domain-level facts
    # (NXDOMAIN, MX presence) change rarely, so the TTL is generous.
    validation_domain_cache_ttl_seconds: int = 21600
    # Per-address results (syntax, provider, disposable, role) are stable for a
    # given string, so they cache far longer than the DNS layer.
    validation_email_cache_ttl_seconds: int = 604800
    validation_phone_cache_ttl_seconds: int = 604800
    # Per-run cap on unique DNS lookups. Protects a large job from hammering
    # resolvers; beyond the cap remaining domains resolve to UNKNOWN, never to
    # a fabricated pass.
    validation_max_unique_dns_lookups: int = 500
    validation_dns_timeout_seconds: float = 2.0
    # SMTP is a distinct signal from MX and is frequently blocked. It ships
    # disabled by default: a probe that cannot reach the MX host records
    # TIMEOUT/UNKNOWN rather than claiming a mailbox exists.
    validation_smtp_enabled: bool = False
    validation_smtp_timeout_seconds: float = 5.0
    # A domain failing this many consecutive probes is circuit-broken so the
    # worker stops burning its budget on a host that is down or blocking us.
    validation_smtp_circuit_breaker_threshold: int = 10
    validation_smtp_circuit_breaker_cooldown_seconds: int = 900
    # Hard ceiling on SMTP probes per verification run. Probing is expensive
    # and reputation-sensitive, so it is always a subset of the run.
    validation_smtp_max_probes_per_run: int = 200
    # Scoring thresholds. The score is a confidence signal, never proof of a
    # real person, so nothing is auto-excluded on score alone.
    validation_score_verified_min: int = 90
    validation_score_likely_valid_min: int = 70
    validation_score_needs_review_min: int = 45
    # Duplicate scoring: agreement weight thresholds.
    validation_duplicate_definite_min: int = 80
    validation_duplicate_possible_min: int = 55
    # Celery chunk size for bulk verification. Chosen so each task slice stays
    # well inside the worker's memory limit regardless of total contact count.
    verification_chunk_size: int = 200
    # Default region used when a stored phone number carries no country code.
    # This is an explicit, configurable assumption - the validator reports when
    # it was applied rather than silently guessing per record.
    validation_default_phone_region: str = "IN"

    @classmethod
    def from_env(cls) -> Settings:
        app_env = os.getenv("APP_ENV", "development").lower()
        # Environments that must never fall back to development credentials.
        requires_explicit = app_env in ("production", "staging")
        dev_jwt_secret = "dev-only-jwt-secret-change-me-32b"
        dev_encryption_key = "dev-only-encryption-key-change-me-32b"
        dev_database_url = "sqlite:///./crcrm.db"

        database_url = os.getenv("DATABASE_URL") or (dev_database_url if not requires_explicit else "")
        if requires_explicit and not database_url:
            raise RuntimeError("DATABASE_URL must be configured in production/staging")
        if requires_explicit and "replace-" in database_url.lower():
            raise RuntimeError("DATABASE_URL still contains a placeholder password in production/staging")

        jwt_secret = os.getenv("JWT_SECRET") or (dev_jwt_secret if not requires_explicit else "")
        if requires_explicit and not jwt_secret:
            raise RuntimeError("JWT_SECRET must be configured in production/staging")
        if requires_explicit:
            _reject_placeholder("JWT_SECRET", jwt_secret)
        jwt_secret = _validate_secret("JWT_SECRET", jwt_secret)

        encryption_key = os.getenv("ENCRYPTION_KEY") or (dev_encryption_key if not requires_explicit else "")
        if requires_explicit and not encryption_key:
            raise RuntimeError("ENCRYPTION_KEY must be configured in production/staging")
        if requires_explicit:
            _reject_placeholder("ENCRYPTION_KEY", encryption_key)
        encryption_key = _validate_secret("ENCRYPTION_KEY", encryption_key)

        default_origins = (
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:5174",
            "http://127.0.0.1:5174",
            "http://localhost:5175",
            "http://127.0.0.1:5175",
        ) if not requires_explicit else ()
        default_hosts = ("localhost", "127.0.0.1", "testserver") if not requires_explicit else ()
        allowed_origins = _comma_separated(os.getenv("ALLOWED_ORIGINS"), default_origins)
        if requires_explicit and not allowed_origins:
            raise RuntimeError("ALLOWED_ORIGINS must be configured in production/staging")
        allowed_hosts = _comma_separated(os.getenv("ALLOWED_HOSTS"), default_hosts)
        if requires_explicit and not allowed_hosts:
            raise RuntimeError("ALLOWED_HOSTS must be configured in production/staging")

        if requires_explicit:
            provider_secrets = {
                "GOOGLE_CLIENT_SECRET": os.getenv("GOOGLE_CLIENT_SECRET", ""),
                "MICROSOFT_CLIENT_SECRET": os.getenv("MICROSOFT_CLIENT_SECRET", ""),
                "ZOHO_CLIENT_SECRET": os.getenv("ZOHO_CLIENT_SECRET", ""),
                "TRANSACTIONAL_SMTP_PASSWORD": os.getenv("TRANSACTIONAL_SMTP_PASSWORD", ""),
            }
            for name, value in provider_secrets.items():
                if value:
                    _reject_placeholder(name, value)

        return cls(
            app_env=app_env,
            database_url=database_url,
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            jwt_secret=jwt_secret,
            access_token_minutes=int(os.getenv("ACCESS_TOKEN_MINUTES", "15")),
            refresh_token_days=int(os.getenv("REFRESH_TOKEN_DAYS", "30")),
            secure_cookies=requires_explicit,
            encryption_key=encryption_key,
            allowed_origins=allowed_origins,
            allowed_hosts=allowed_hosts,
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
            google_redirect_uri=os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/v1/senders/google/callback"),
            google_sender_redirect_uri=os.getenv(
                "GOOGLE_SENDER_REDIRECT_URI",
                "http://localhost:8000/api/v1/senders/google/oauth/callback",
            ),
            google_workspace_redirect_uri=os.getenv(
                "GOOGLE_WORKSPACE_REDIRECT_URI",
                "http://localhost:8000/api/v1/provider-connections/google/callback",
            ),
            microsoft_client_id=os.getenv("MICROSOFT_CLIENT_ID", ""),
            microsoft_client_secret=os.getenv("MICROSOFT_CLIENT_SECRET", ""),
            microsoft_tenant_id=os.getenv("MICROSOFT_TENANT_ID", "common"),
            microsoft_authority=os.getenv("MICROSOFT_AUTHORITY", "https://login.microsoftonline.com"),
            microsoft_redirect_uri=os.getenv("MICROSOFT_REDIRECT_URI", "http://localhost:8000/api/v1/senders/microsoft/callback"),
            microsoft_sender_redirect_uri=os.getenv(
                "MICROSOFT_SENDER_REDIRECT_URI",
                "http://localhost:8000/api/v1/senders/microsoft/oauth/callback",
            ),
            microsoft_workspace_redirect_uri=os.getenv(
                "MICROSOFT_WORKSPACE_REDIRECT_URI",
                "http://localhost:8000/api/v1/provider-connections/microsoft/callback",
            ),
            zoho_client_id=os.getenv("ZOHO_CLIENT_ID", ""),
            zoho_client_secret=os.getenv("ZOHO_CLIENT_SECRET", ""),
            zoho_authority=os.getenv("ZOHO_AUTHORITY", "https://accounts.zoho.com"),
            zoho_sender_redirect_uri=os.getenv(
                "ZOHO_SENDER_REDIRECT_URI",
                "http://localhost:8000/api/v1/senders/zoho/oauth/callback",
            ),
            smtp_allow_private_hosts=_as_bool(os.getenv("SMTP_ALLOW_PRIVATE_HOSTS"), False),
            smtp_allow_plaintext_auth=_as_bool(os.getenv("SMTP_ALLOW_PLAINTEXT_AUTH"), False),
            smtp_allow_insecure_ports=_as_bool(os.getenv("SMTP_ALLOW_INSECURE_PORTS"), False),
            transactional_smtp_host=os.getenv("TRANSACTIONAL_SMTP_HOST", ""),
            transactional_smtp_port=int(os.getenv("TRANSACTIONAL_SMTP_PORT", "587")),
            transactional_smtp_username=os.getenv("TRANSACTIONAL_SMTP_USERNAME", ""),
            transactional_smtp_password=os.getenv("TRANSACTIONAL_SMTP_PASSWORD", ""),
            transactional_email_from=os.getenv("TRANSACTIONAL_EMAIL_FROM", ""),
            password_reset_url=os.getenv("PASSWORD_RESET_URL", "http://localhost:5173/reset-password"),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8000"),
            webhook_secrets=_webhook_secrets(os.getenv("WEBHOOK_SECRETS", "")),
            ai_provider=os.getenv("AI_PROVIDER", "mock"),
            ai_monthly_budget_usd=float(os.getenv("AI_MONTHLY_BUDGET_USD", "0")),
            ai_monthly_generation_limit=int(os.getenv("AI_MONTHLY_GENERATION_LIMIT", "0")),
            ai_request_timeout_ms=int(os.getenv("AI_REQUEST_TIMEOUT_MS", "60000")),
            ops_metrics_ttl_seconds=int(os.getenv("OPS_METRICS_TTL_SECONDS", "86400")),
            ops_storage_path=os.getenv("OPS_STORAGE_PATH", "."),
            storage_root=os.getenv("STORAGE_ROOT", ".local_uploads"),
            import_file_retention_days=int(os.getenv("IMPORT_FILE_RETENTION_DAYS", "30")),
            import_file_retention_days_min=int(os.getenv("IMPORT_FILE_RETENTION_DAYS_MIN", "1")),
            import_file_retention_days_max=int(os.getenv("IMPORT_FILE_RETENTION_DAYS_MAX", "365")),
            import_stale_job_seconds=int(os.getenv("IMPORT_STALE_JOB_SECONDS", "300")),
            import_reclaim_interval_seconds=int(
                os.getenv("IMPORT_RECLAIM_INTERVAL_SECONDS", "60")
            ),
            backup_dir=os.getenv("BACKUP_DIR", "./backups"),
            clerk_issuer=os.getenv("CLERK_ISSUER", "").rstrip("/"),
            clerk_jwks_url=os.getenv("CLERK_JWKS_URL", "").rstrip("/"),
            clerk_audience=os.getenv("CLERK_AUDIENCE") or None,
            legacy_auth_enabled=_as_bool(os.getenv("AUTH_LEGACY_ENABLED"), True),
            provider_credential_refresh_margin_minutes=int(os.getenv("PROVIDER_CREDENTIAL_REFRESH_MARGIN_MINUTES", "5")),
            provider_credential_refresh_min_interval_minutes=int(os.getenv("PROVIDER_CREDENTIAL_REFRESH_MIN_INTERVAL_MINUTES", "30")),
            mailbox_sync_inline=_as_bool(os.getenv("MAILBOX_SYNC_INLINE"), False),
            sender_bulk_async_threshold=int(os.getenv("SENDER_BULK_ASYNC_THRESHOLD", "500")),
            sender_bulk_inline=_as_bool(os.getenv("SENDER_BULK_INLINE"), False),
            sender_health_manual_min_interval_seconds=int(os.getenv("SENDER_HEALTH_MANUAL_MIN_INTERVAL_SECONDS", "60")),
            sender_health_lock_ttl_seconds=int(os.getenv("SENDER_HEALTH_LOCK_TTL_SECONDS", "300")),
            health_history_retention_days=int(os.getenv("HEALTH_HISTORY_RETENTION_DAYS", "90")),
            validation_domain_cache_ttl_seconds=int(os.getenv("VALIDATION_DOMAIN_CACHE_TTL_SECONDS", "21600")),
            validation_email_cache_ttl_seconds=int(os.getenv("VALIDATION_EMAIL_CACHE_TTL_SECONDS", "604800")),
            validation_phone_cache_ttl_seconds=int(os.getenv("VALIDATION_PHONE_CACHE_TTL_SECONDS", "604800")),
            validation_max_unique_dns_lookups=int(os.getenv("VALIDATION_MAX_UNIQUE_DNS_LOOKUPS", "500")),
            validation_dns_timeout_seconds=float(os.getenv("VALIDATION_DNS_TIMEOUT_SECONDS", "2.0")),
            validation_smtp_enabled=_as_bool(os.getenv("VALIDATION_SMTP_ENABLED"), False),
            validation_smtp_timeout_seconds=float(os.getenv("VALIDATION_SMTP_TIMEOUT_SECONDS", "5.0")),
            validation_smtp_circuit_breaker_threshold=int(os.getenv("VALIDATION_SMTP_CIRCUIT_BREAKER_THRESHOLD", "10")),
            validation_smtp_circuit_breaker_cooldown_seconds=int(os.getenv("VALIDATION_SMTP_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "900")),
            validation_smtp_max_probes_per_run=int(os.getenv("VALIDATION_SMTP_MAX_PROBES_PER_RUN", "200")),
            validation_score_verified_min=int(os.getenv("VALIDATION_SCORE_VERIFIED_MIN", "90")),
            validation_score_likely_valid_min=int(os.getenv("VALIDATION_SCORE_LIKELY_VALID_MIN", "70")),
            validation_score_needs_review_min=int(os.getenv("VALIDATION_SCORE_NEEDS_REVIEW_MIN", "45")),
            validation_duplicate_definite_min=int(os.getenv("VALIDATION_DUPLICATE_DEFINITE_MIN", "80")),
            validation_duplicate_possible_min=int(os.getenv("VALIDATION_DUPLICATE_POSSIBLE_MIN", "55")),
            verification_chunk_size=int(os.getenv("VERIFICATION_CHUNK_SIZE", "200")),
            validation_default_phone_region=os.getenv("VALIDATION_DEFAULT_PHONE_REGION", "IN"),
        )


settings = Settings.from_env()

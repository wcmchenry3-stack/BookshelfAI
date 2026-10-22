import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.ai.models_allowlist import (
    ALLOWED_MODELS,
    MAX_TOKENS_BOUNDS,
    TIMEOUT_S_BOUNDS,
)

# Supabase's transaction pooler (transaction-mode pooling) doesn't support
# asyncpg's server-side prepared statements, which asyncpg uses by default. It
# passes a single probe, then fails intermittently under concurrency with
# "prepared statement ... does not exist" / DuplicatePreparedStatementError.
# The session pooler on port 5432 must be used instead.
_SUPABASE_TRANSACTION_POOLER_PORT = 6543

# RFC 9110 field-name token characters.
_HEADER_NAME_PATTERN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")

# ORIGIN_AUTH_SECRET, when set: printable ASCII (it travels as a header value),
# long enough not to be guessed. Generate with e.g. `openssl rand -hex 32`.
_ORIGIN_AUTH_SECRET_MIN_LENGTH = 32
_PRINTABLE_ASCII_PATTERN = re.compile(r"[\x20-\x7e]+")


class Settings(BaseSettings):
    # hide_input_in_errors: a validation error must never echo a secret value
    # (e.g. ORIGIN_AUTH_SECRET) into boot logs.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", hide_input_in_errors=True
    )

    # Database
    database_url: str
    # Bounded async pool per instance. Render zero-downtime deploys run 2
    # instances concurrently during a deploy, plus a one-off `alembic upgrade
    # head` connection: 2 * (db_pool_size + db_max_overflow) + 1 must stay
    # under the small Supabase session pooler's 15-client cap.
    # 2 * (3 + 2) + 1 = 11 <= 15.
    db_pool_size: int = 3
    db_max_overflow: int = 2

    # Auth
    google_client_id: str = ""
    google_ios_client_id: str = ""
    google_android_client_id: str = ""
    google_client_secret: str = ""
    jwt_private_key: str = ""
    jwt_public_key: str = ""
    jwt_expiry_hours: int = 24
    refresh_token_expiry_days: int = 7
    allowed_emails: str = ""

    # External APIs
    openai_api_key: str = ""
    openai_max_tokens: int = 512
    # Multi-book scan: one photo can hold up to scan_max_books covers or spines.
    # The JSON for 15 books needs far more room than openai_max_tokens allows,
    # and high-detail images take longer to process, hence separate knobs.
    scan_max_books: int = 15
    scan_max_tokens: int = Field(
        default=2048, ge=MAX_TOKENS_BOUNDS[0], le=MAX_TOKENS_BOUNDS[1]
    )
    scan_timeout_seconds: float = Field(
        default=45.0, ge=TIMEOUT_S_BOUNDS[0], le=TIMEOUT_S_BOUNDS[1]
    )
    # SCAN_MODEL / SCAN_ENHANCED_MODEL are the registry defaults and seed the
    # ai_settings rows scan.standard.model / scan.enhanced.model.
    scan_model: str = "gpt-4o-mini"
    # "Enhanced" scan — a stronger model the user can opt into when the standard
    # scan misses books. Each successful enhanced scan costs one credit; new
    # users start with enhanced_scan_free_credits.
    scan_enhanced_model: str = "gpt-4o"
    enhanced_scan_free_credits: int = 5
    # Kill switches for /scan. INITIAL DEFAULTS ONLY: since #526 they seed the
    # ai_settings rows scan.standard.enabled / scan.enhanced.enabled on first
    # boot, and after that the DB row is the source of truth (change it with
    # `python -m app.ai.settings set ...`, see app/ai/settings.py). They are
    # also the fallback when a row is missing. A disabled scan is rejected
    # before the image is processed, Turnstile is called or the provider is
    # contacted (the framework has already received the multipart body);
    # scan.enhanced.enabled=false rejects only enhanced scans.
    scan_enabled: bool = True
    scan_enhanced_enabled: bool = True
    # LEGACY (#509): read only by app/services/scan_quota.py, which /scan no
    # longer calls since #528 (deleted in #540). The per-user allowances now
    # come from the ai_settings quota.* keys seeded from SCAN_FREE_* and
    # SCAN_PREMIUM_* below.
    scan_daily_limit_per_user: int = Field(default=20, ge=1, le=1000)
    # INITIAL DEFAULT ONLY: seeds the ai_settings row scan.global_daily_limit
    # (same bounds): scans per UTC day across all users, checked before any
    # per-user reservation (503 scan_capacity). Approximate under concurrency.
    scan_global_daily_limit: int = Field(default=2000, ge=1, le=1_000_000)
    # Per-user scan allowances by tier (#528). INITIAL DEFAULTS ONLY: they seed
    # the ai_settings rows quota.<tier>.daily / quota.<tier>.monthly (same
    # bounds), which are the source of truth after first boot. Daily windows
    # are UTC days, monthly windows UTC calendar months. 0 refuses every scan
    # for that tier. Launch values (OD-1): Free 5 a day and 5 a month.
    scan_free_daily_limit: int = Field(default=5, ge=0, le=1000)
    scan_free_monthly_limit: int = Field(default=5, ge=0, le=100_000)
    scan_premium_daily_limit: int = Field(default=30, ge=0, le=1000)
    scan_premium_monthly_limit: int = Field(default=300, ge=0, le=100_000)
    # Decompression-bomb guard for /scan uploads: an image whose decoded pixel
    # count (width x height) is above this is refused with 413
    # image_too_large_pixels before it is fully decoded. Bounded so a typo
    # cannot disable the guard. INITIAL DEFAULT ONLY: seeds the ai_settings row
    # scan.max_image_pixels (same bounds), which is the source of truth after
    # first boot; also the fallback when that row is missing.
    scan_max_image_pixels: int = Field(default=40_000_000, ge=1_000_000, le=100_000_000)
    # How many /scan image normalisations (decode + resize) may run at once in
    # worker threads, per process. Each can hold a few hundred MB for a
    # full-size image, so this bounds peak memory; extra requests wait.
    # Stays env (not ai_settings): it sizes a process-wide CapacityLimiter
    # created at import, so a DB change could not take effect without a restart.
    scan_normalise_concurrency: int = Field(default=1, ge=1, le=8)
    # How long a process reuses the ai_settings rows it last read (seconds).
    # The cache is per process (per uvicorn worker): a change made with the
    # CLI reaches every process within this time, so two processes (or two
    # instances during a deploy) may disagree for up to this long.
    ai_settings_cache_ttl_seconds: int = Field(default=30, ge=1, le=300)
    # How old the cached ai_settings rows may be and still be served when a
    # read fails. Past this, scans fail closed (503 ai_unavailable). Must be
    # at least the cache TTL (checked at boot).
    ai_settings_max_stale_seconds: int = Field(default=300, ge=1, le=3600)
    google_books_api_key: str = ""
    open_library_base_url: str = "https://openlibrary.org"

    # Cloudflare Turnstile — bot protection on /scan.
    # turnstile_required=true (default) means every /scan request must supply a valid
    # cf-turnstile-response token.  If turnstile_secret_key is absent at startup the
    # app refuses to start, preventing a misconfigured production deploy.
    # Set TURNSTILE_REQUIRED=false to explicitly opt out (dev / test environments).
    turnstile_secret_key: str = ""
    turnstile_required: bool = True

    # ClamAV antivirus — optional malware scan on uploaded images.
    # Requires a running ClamAV daemon (clamd) and pyclamd installed.
    # Set CLAMAV_ENABLED=true when the Render instance has sufficient RAM (~300 MB).
    clamav_enabled: bool = False
    clamav_host: str = "localhost"
    clamav_port: int = 3310

    # Testing (development/test only — the /auth/test-login route that consumes
    # this is not even registered when settings.is_hardened is True, i.e. in
    # staging or production)
    test_auth_secret: str = ""

    # Observability
    sentry_dsn: str = ""
    # Set automatically by Render on every deploy (RENDER_GIT_COMMIT). Empty
    # locally and in CI — there sentry-sdk falls back to its own detection (a
    # bare git SHA), under a non-production `environment`.
    render_git_commit: str = ""

    # App
    environment: str = "development"
    cors_origins: list[str] = []
    rate_limit_scan: str = "10/minute"
    rate_limit_auth: str = "5/minute"
    rate_limit_books_search: str = "30/minute"
    rate_limit_writes: str = "60/minute"
    rate_limit_reads: str = "120/minute"
    rate_limit_health: str = "60/minute"
    # Allowlist of Host header values accepted when settings.is_hardened is True
    # (staging and production). Override via TRUSTED_HOSTS env var: '["api.example.com"]'
    trusted_hosts: list[str] = ["*"]

    # Client-IP trust (app/core/client_ip.py). "none": the socket peer, every
    # forwarding header ignored. "cloudflare": a single valid CF-Connecting-IP
    # value, else the socket peer. Any other value fails boot. When unset (or
    # empty) it defaults to "cloudflare" in hardened environments and "none"
    # otherwise — see _default_trusted_proxy_mode; after validation it always
    # holds one of the two values.
    trusted_proxy_mode: Literal["cloudflare", "none"] | None = None

    # Origin lockdown (app/core/origin_auth.py). When origin_auth_secret is
    # set, every request except /health must carry origin_auth_header with
    # exactly that value, or it gets 403. Empty disables the check.
    origin_auth_header: str = "X-Origin-Auth"
    origin_auth_secret: str = ""

    @field_validator("cors_origins")
    @classmethod
    def no_wildcard_origins(cls, v: list[str]) -> list[str]:
        if "*" in v:
            raise ValueError("Wildcard '*' is not permitted in CORS_ORIGINS")
        return v

    @field_validator("scan_model", "scan_enhanced_model")
    @classmethod
    def allowed_openai_model(cls, v: str, info) -> str:
        if v not in ALLOWED_MODELS["openai"]:
            raise ValueError(
                f"{info.field_name.upper()}={v!r} is not an allowed OpenAI model; "
                f"allowed: {', '.join(sorted(ALLOWED_MODELS['openai']))}"
            )
        return v

    @field_validator("origin_auth_header")
    @classmethod
    def valid_header_name(cls, v: str) -> str:
        if not _HEADER_NAME_PATTERN.fullmatch(v):
            raise ValueError("ORIGIN_AUTH_HEADER must be a valid HTTP header name")
        return v

    @field_validator("trusted_proxy_mode", mode="before")
    @classmethod
    def empty_proxy_mode_is_unset(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("origin_auth_secret")
    @classmethod
    def valid_origin_auth_secret(cls, v: str) -> str:
        # Messages describe the rule only; the value is never echoed.
        if not v:
            return v
        if v != v.strip():
            raise ValueError(
                "ORIGIN_AUTH_SECRET must not have leading or trailing whitespace"
            )
        if not _PRINTABLE_ASCII_PATTERN.fullmatch(v):
            raise ValueError("ORIGIN_AUTH_SECRET must be printable ASCII")
        if len(v) < _ORIGIN_AUTH_SECRET_MIN_LENGTH:
            raise ValueError(
                "ORIGIN_AUTH_SECRET must be at least "
                f"{_ORIGIN_AUTH_SECRET_MIN_LENGTH} characters"
            )
        return v

    @model_validator(mode="after")
    def _max_stale_not_below_ttl(self) -> "Settings":
        if self.ai_settings_max_stale_seconds < self.ai_settings_cache_ttl_seconds:
            raise ValueError(
                "AI_SETTINGS_MAX_STALE_SECONDS "
                f"({self.ai_settings_max_stale_seconds}) must be at least "
                "AI_SETTINGS_CACHE_TTL_SECONDS "
                f"({self.ai_settings_cache_ttl_seconds})"
            )
        return self

    @model_validator(mode="after")
    def _default_trusted_proxy_mode(self) -> "Settings":
        """Unset TRUSTED_PROXY_MODE: "cloudflare" when hardened, else "none".

        Hardened deployments are served through the Cloudflare edge, so the
        safe default there is to key rate limits on CF-Connecting-IP; locally
        there is no proxy and every forwarding header is ignored.
        """
        if self.trusted_proxy_mode is None:
            self.trusted_proxy_mode = "cloudflare" if self.is_hardened else "none"
        return self

    @field_validator("database_url")
    @classmethod
    def reject_transaction_pooler_port(cls, v: str) -> str:
        port = urlsplit(v).port
        if port == _SUPABASE_TRANSACTION_POOLER_PORT:
            raise ValueError(
                f"DATABASE_URL uses port {_SUPABASE_TRANSACTION_POOLER_PORT} "
                "(Supabase's transaction pooler), which breaks asyncpg's "
                "server-side prepared statements. Use the session pooler on "
                "port 5432 instead."
            )
        return v

    @property
    def is_hardened(self) -> bool:
        """True for any environment that must be treated as security-sensitive.

        A public `staging` deployment is just as reachable by attackers as
        `production`, so it must get the same hardening (TrustedHost
        enforcement, no /docs, no debug/test-only routes, HSTS) even though
        it's tagged separately in Sentry. Which client-IP header is trusted is
        a separate setting, ``trusted_proxy_mode``.
        """
        return self.environment not in {"development", "test"}

    @property
    def sentry_release(self) -> str | None:
        """Sentry release id, so an issue can be tied to the deploy that caused it.

        ``None`` does not mean "no release": the SDK then uses its default
        detection. On Render the commit is always set, so every deployed event
        carries exactly one format, ``bookshelf-api@<sha>``.
        """
        sha = self.render_git_commit.strip()
        return f"bookshelf-api@{sha}" if sha else None

    @property
    def async_database_url(self) -> str:
        # Normalize any postgres:// or postgresql:// scheme to postgresql+asyncpg://
        # and sqlite:// to sqlite+aiosqlite:// (used by schema-check CI with SQLite)
        import re

        url = re.sub(r"^postgres(ql)?://", "postgresql+asyncpg://", self.database_url)
        url = re.sub(r"^sqlite:///", "sqlite+aiosqlite:///", url)
        return url

    @property
    def google_client_ids(self) -> list[str]:
        """All valid Google OAuth client IDs (web, iOS, Android)."""
        return [
            cid
            for cid in [
                self.google_client_id,
                self.google_ios_client_id,
                self.google_android_client_id,
            ]
            if cid
        ]

    @property
    def allowed_emails_list(self) -> list[str]:
        return [e.strip() for e in self.allowed_emails.split(",") if e.strip()]


settings = Settings()

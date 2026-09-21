from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Supabase's transaction pooler (transaction-mode pooling) doesn't support
# asyncpg's server-side prepared statements, which asyncpg uses by default. It
# passes a single probe, then fails intermittently under concurrency with
# "prepared statement ... does not exist" / DuplicatePreparedStatementError.
# The session pooler on port 5432 must be used instead.
_SUPABASE_TRANSACTION_POOLER_PORT = 6543


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

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

    @field_validator("cors_origins")
    @classmethod
    def no_wildcard_origins(cls, v: list[str]) -> list[str]:
        if "*" in v:
            raise ValueError("Wildcard '*' is not permitted in CORS_ORIGINS")
        return v

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
        enforcement, no /docs, no debug/test-only routes, HSTS, trusting
        CF-Connecting-IP) even though it's tagged separately in Sentry.
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

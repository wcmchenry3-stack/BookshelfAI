"""Guard tests for render.yaml — dev/prod topology and secret hygiene.

Prod (`bookshelf-api`, `bookshelf-web`, branch `main`) must never reference a
Render-managed database — production data lives in Supabase, configured only
through dashboard secrets (`sync: false`). Dev (`bookshelf-api-dev`,
`bookshelf-web-dev`, branch `dev`) keeps using the Render Postgres
`bookshelf-db`, and runs with `ENVIRONMENT=staging` (not `development`) so it
exercises the same hardened code path as prod (`Settings.is_hardened`, #448).
These tests also guard against secret-shaped env vars carrying a literal
`value` and against literal Postgres connection strings anywhere in the file.
See docs/RENDER.md for the full topology.
"""

import re
from pathlib import Path

import yaml

RENDER_YAML_PATH = Path(__file__).resolve().parents[3] / "render.yaml"

# Keys that must never carry a literal `value` — only `sync: false` (or
# `fromDatabase` for dev's DATABASE_URL). Substrings are matched anywhere in
# the key name; the exact-key set covers names that don't match a substring.
_SECRET_KEY_SUBSTRINGS = ("SECRET", "PRIVATE_KEY", "API_KEY")
_SECRET_KEY_EXACT = {"SENTRY_DSN", "TURNSTILE_SECRET_KEY"}

_POSTGRES_URL_PATTERN = re.compile(r"postgres(ql)?(\+\w+)?://")

_REQUIRED_CSP_ORIGINS = (
    "https://*.ingest.us.sentry.io",
    "https://feedback-worker.wcmchenry3.workers.dev",
)


def _load_render_yaml() -> dict:
    return yaml.safe_load(RENDER_YAML_PATH.read_text(encoding="utf-8"))


def _service(config: dict, name: str) -> dict:
    for service in config["services"]:
        if service["name"] == name:
            return service
    raise AssertionError(f"service {name!r} not found in render.yaml")


def _env_var(service: dict, key: str) -> dict:
    for env_var in service.get("envVars", []):
        if env_var["key"] == key:
            return env_var
    raise AssertionError(f"envVar {key!r} not found on service {service['name']!r}")


def _header_value(service: dict, name: str) -> str:
    for header in service.get("headers", []):
        if header["name"] == name:
            return header["value"]
    raise AssertionError(f"header {name!r} not found on service {service['name']!r}")


def _is_secret_key(key: str) -> bool:
    return key in _SECRET_KEY_EXACT or any(p in key for p in _SECRET_KEY_SUBSTRINGS)


# --- Prod API (bookshelf-api) -----------------------------------------------


def test_prod_api_database_url_is_dashboard_secret():
    config = _load_render_yaml()
    prod_api = _service(config, "bookshelf-api")
    database_url = _env_var(prod_api, "DATABASE_URL")
    assert database_url.get("sync") is False
    assert "fromDatabase" not in database_url
    assert "value" not in database_url


def test_prod_api_environment_is_production():
    config = _load_render_yaml()
    prod_api = _service(config, "bookshelf-api")
    assert _env_var(prod_api, "ENVIRONMENT")["value"] == "production"


def test_prod_api_deploys_from_main_without_autodeploy():
    config = _load_render_yaml()
    prod_api = _service(config, "bookshelf-api")
    assert prod_api["branch"] == "main"
    assert prod_api["autoDeploy"] is False


def test_prod_api_health_check_path():
    config = _load_render_yaml()
    prod_api = _service(config, "bookshelf-api")
    assert prod_api["healthCheckPath"] == "/health"


# --- No main-branch service may use a Render-managed database --------------


def test_no_main_branch_service_uses_from_database():
    config = _load_render_yaml()
    main_branch_services = [s for s in config["services"] if s.get("branch") == "main"]
    assert main_branch_services, "expected at least one main-branch service"
    for service in main_branch_services:
        for env_var in service.get("envVars", []):
            assert "fromDatabase" not in env_var, (
                f"{service['name']} envVar {env_var.get('key')!r} uses "
                "fromDatabase on a main-branch (prod) service — prod must use "
                "Supabase, never a Render database"
            )


# --- Dev API (bookshelf-api-dev) --------------------------------------------


def test_dev_api_environment_is_staging():
    config = _load_render_yaml()
    dev_api = _service(config, "bookshelf-api-dev")
    value = _env_var(dev_api, "ENVIRONMENT")["value"]
    # staging, not development/production: Settings.is_hardened (#448) treats
    # everything except development/test as hardened, and dev must exercise
    # that same hardened path.
    assert value == "staging"


def test_dev_api_database_url_comes_from_render_database():
    config = _load_render_yaml()
    dev_api = _service(config, "bookshelf-api-dev")
    database_url = _env_var(dev_api, "DATABASE_URL")
    assert database_url["fromDatabase"]["name"] == "bookshelf-db"


def test_dev_api_deploys_from_dev_branch():
    config = _load_render_yaml()
    dev_api = _service(config, "bookshelf-api-dev")
    assert dev_api["branch"] == "dev"


# --- Prod web (bookshelf-web) ------------------------------------------------


def test_prod_web_deploys_from_main_without_autodeploy():
    config = _load_render_yaml()
    prod_web = _service(config, "bookshelf-web")
    assert prod_web["branch"] == "main"
    assert prod_web["autoDeploy"] is False


def test_prod_web_points_at_prod_api():
    config = _load_render_yaml()
    prod_web = _service(config, "bookshelf-web")
    assert (
        _env_var(prod_web, "EXPO_PUBLIC_API_URL")["value"]
        == "https://bookshelfapi.buffingchi.com"
    )


def test_prod_web_csp_references_prod_api_and_not_dev():
    config = _load_render_yaml()
    prod_web = _service(config, "bookshelf-web")
    csp = _header_value(prod_web, "Content-Security-Policy")
    assert "https://bookshelfapi.buffingchi.com" in csp
    assert "-dev" not in csp


def test_prod_web_csp_includes_sentry_and_feedback_worker_origins():
    config = _load_render_yaml()
    prod_web = _service(config, "bookshelf-web")
    csp = _header_value(prod_web, "Content-Security-Policy")
    for origin in _REQUIRED_CSP_ORIGINS:
        assert origin in csp, f"bookshelf-web CSP is missing {origin!r}"


# --- Dev web (bookshelf-web-dev) ---------------------------------------------


def test_dev_web_csp_does_not_reference_prod_api():
    config = _load_render_yaml()
    dev_web = _service(config, "bookshelf-web-dev")
    csp = _header_value(dev_web, "Content-Security-Policy")
    assert "https://bookshelfapi.buffingchi.com" not in csp


def test_dev_web_csp_includes_sentry_and_feedback_worker_origins():
    config = _load_render_yaml()
    dev_web = _service(config, "bookshelf-web-dev")
    csp = _header_value(dev_web, "Content-Security-Policy")
    for origin in _REQUIRED_CSP_ORIGINS:
        assert origin in csp, f"bookshelf-web-dev CSP is missing {origin!r}"


# --- Secret-leak guards -------------------------------------------------------


def test_no_env_var_literal_value_leaks_a_supabase_connection_string():
    config = _load_render_yaml()
    for service in config["services"]:
        for env_var in service.get("envVars", []):
            value = env_var.get("value")
            if not isinstance(value, str):
                continue
            lowered = value.lower()
            assert "supabase" not in lowered, (
                f"{service['name']}.{env_var['key']} has a literal value "
                "mentioning 'supabase' — secrets belong in the dashboard only"
            )
            assert "pooler" not in lowered, (
                f"{service['name']}.{env_var['key']} has a literal value "
                "mentioning 'pooler' — secrets belong in the dashboard only"
            )


def test_no_env_var_literal_value_matches_a_postgres_connection_string():
    config = _load_render_yaml()
    for service in config["services"]:
        for env_var in service.get("envVars", []):
            value = env_var.get("value")
            if not isinstance(value, str):
                continue
            assert not _POSTGRES_URL_PATTERN.search(value), (
                f"{service['name']}.{env_var['key']} has a literal value that "
                "looks like a Postgres connection string — this belongs in "
                "the dashboard (sync: false) or fromDatabase, never a literal "
                "value in render.yaml"
            )


def test_secret_shaped_env_vars_never_carry_a_literal_value():
    """DATABASE_URL on any main-branch (prod) service, and any envVar whose key
    looks like a secret (*SECRET*, *PRIVATE_KEY*, *API_KEY*, SENTRY_DSN,
    TURNSTILE_SECRET_KEY), must be `sync: false` (or `fromDatabase` for dev's
    DATABASE_URL) — never a literal `value`."""
    config = _load_render_yaml()
    for service in config["services"]:
        for env_var in service.get("envVars", []):
            key = env_var["key"]
            is_prod_database_url = (
                key == "DATABASE_URL" and service.get("branch") == "main"
            )
            if not (is_prod_database_url or _is_secret_key(key)):
                continue
            assert "value" not in env_var, (
                f"{service['name']}.{key} is a secret-shaped key but carries "
                "a literal 'value' — it must be sync:false (or fromDatabase "
                "for dev's DATABASE_URL) only"
            )

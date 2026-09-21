"""Guard tests for render.yaml — dev/prod topology and secret hygiene.

Prod (`bookshelf-api`, `bookshelf-web`) must never reference a Render-managed
database — production data lives in Supabase, configured only through
dashboard secrets (`sync: false`). Dev (`bookshelf-api-dev`,
`bookshelf-web-dev`) keeps using the Render Postgres `bookshelf-db`. See
docs/RENDER.md for the full topology.
"""

from pathlib import Path

import yaml

RENDER_YAML_PATH = Path(__file__).resolve().parents[3] / "render.yaml"


def _load_render_yaml() -> dict:
    return yaml.safe_load(RENDER_YAML_PATH.read_text())


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


# --- Prod API (bookshelf-api) -----------------------------------------------


def test_prod_api_has_no_from_database_anywhere():
    config = _load_render_yaml()
    prod_api = _service(config, "bookshelf-api")
    for env_var in prod_api.get("envVars", []):
        assert "fromDatabase" not in env_var, (
            f"bookshelf-api envVar {env_var.get('key')!r} uses fromDatabase "
            "— prod must use Supabase, never a Render database"
        )


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


# --- Dev API (bookshelf-api-dev) --------------------------------------------


def test_dev_api_environment_is_not_production():
    config = _load_render_yaml()
    dev_api = _service(config, "bookshelf-api-dev")
    assert _env_var(dev_api, "ENVIRONMENT")["value"] != "production"


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


# --- Dev web (bookshelf-web-dev) ---------------------------------------------


def test_dev_web_csp_does_not_reference_prod_api():
    config = _load_render_yaml()
    dev_web = _service(config, "bookshelf-web-dev")
    csp = _header_value(dev_web, "Content-Security-Policy")
    assert "https://bookshelfapi.buffingchi.com" not in csp


# --- Secret-leak guard --------------------------------------------------------


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

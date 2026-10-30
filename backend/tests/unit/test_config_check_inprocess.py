"""In-process behaviour tests for ``app.core.config_check`` (#765).

``tests/unit/test_blank_settings.py`` runs the module as a subprocess, which
coverage does not see. These call the functions directly, with the settings
checks stubbed, and assert what the operator would read: the ``NAME: reason``
lines, the exit code, and that no configuration value is echoed.
"""

import pytest
from pydantic import BaseModel, ValidationError, field_validator

from app.core import catalog_token, config_check
from app.core import config as config_module


@pytest.fixture(autouse=True)
def _reset_raw_values(monkeypatch):
    monkeypatch.setattr(config_check, "_raw_values", [])


class _Model(BaseModel):
    scan_model: str

    @field_validator("scan_model")
    @classmethod
    def _check(cls, v):
        raise ValueError("SCAN_MODEL='bogus' is not an allowed model")


def _validation_error() -> ValidationError:
    with pytest.raises(ValidationError) as ei:
        _Model(scan_model="bogus")
    return ei.value


def test_validation_problems_name_the_variable_and_redact_the_value():
    problems = config_check._validation_problems(_validation_error())
    assert problems == ["SCAN_MODEL: SCAN_MODEL=<redacted> is not an allowed model"]


def test_validation_problems_without_location_use_settings_label():
    class _Err:
        def errors(self, **_):
            return [{"loc": (), "msg": "Value error, broken thing"}]

    assert config_check._validation_problems(_Err()) == ["settings: broken thing"]


def test_reason_keeps_first_line_only_and_redacts_known_secret_fragments(monkeypatch):
    monkeypatch.setattr(config_check, "_raw_values", ["prefix-hunter2secret-suffix"])
    out = config_check._reason("bad value hunter2secret here\nsecond line")
    assert "hunter2secret" not in out
    assert "second line" not in out
    assert out == "bad value <redacted> here"


def test_reason_leaves_variable_names_and_short_tokens(monkeypatch):
    monkeypatch.setattr(config_check, "_raw_values", ["SCAN_MODEL abc"])
    out = config_check._reason("SCAN_MODEL abc is wrong")
    assert out == "SCAN_MODEL abc is wrong"


def test_collect_raw_values_includes_environment_and_dotenv(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("FROM_DOTENV=dotenv-value-1\nEMPTY=\n")
    monkeypatch.setenv("FROM_ENV_765", "env-value-765")
    monkeypatch.setenv("EMPTY_ENV_765", "")
    values = config_check._collect_raw_values()
    assert "dotenv-value-1" in values
    assert "env-value-765" in values
    assert "" not in values


def test_collect_problems_reports_settings_validation_error(monkeypatch):
    err = _validation_error()

    def boom():
        raise err

    monkeypatch.setattr(config_module, "Settings", boom)
    assert config_check.collect_problems() == [
        "SCAN_MODEL: SCAN_MODEL=<redacted> is not an allowed model"
    ]


def test_collect_problems_reports_blank_setting_error(monkeypatch):
    def boom():
        raise config_module.BlankSettingError("FOO is set but blank")

    monkeypatch.setattr(config_module, "Settings", boom)
    assert config_check.collect_problems() == ["settings: FOO is set but blank"]


def test_collect_problems_reports_turnstile_and_catalog_token(monkeypatch):
    monkeypatch.setattr(config_module, "Settings", lambda: object())

    def turnstile(_cfg):
        raise RuntimeError("TURNSTILE_SECRET_KEY must be configured")

    def catalog(_cfg):
        raise catalog_token.CatalogTokenConfigError("JWT_PRIVATE_KEY missing")

    monkeypatch.setattr(config_module, "check_turnstile_configured", turnstile)
    monkeypatch.setattr(catalog_token, "check_configured", catalog)
    assert config_check.collect_problems() == [
        "TURNSTILE_SECRET_KEY: TURNSTILE_SECRET_KEY must be configured",
        "JWT_PRIVATE_KEY: JWT_PRIVATE_KEY missing",
    ]


def test_collect_problems_empty_when_checks_pass(monkeypatch):
    monkeypatch.setattr(config_module, "Settings", lambda: object())
    monkeypatch.setattr(config_module, "check_turnstile_configured", lambda _c: None)
    monkeypatch.setattr(catalog_token, "check_configured", lambda _c=None: None)
    assert config_check.collect_problems() == []


def test_main_prints_ok_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(config_check, "collect_problems", list)
    assert config_check.main() == 0
    assert capsys.readouterr().out.strip() == "OK"


def test_main_prints_each_problem_and_returns_one(monkeypatch, capsys):
    monkeypatch.setattr(config_check, "collect_problems", lambda: ["A_B: x", "C_D: y"])
    assert config_check.main() == 1
    out = capsys.readouterr().out.splitlines()
    assert out == ["Invalid configuration:", "  A_B: x", "  C_D: y"]

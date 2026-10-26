"""``python -m app.core.config_check``: validate the environment like boot does.

Builds ``Settings`` (reading the process environment and ``.env``, exactly as
the app does at import) and runs the checks that need no database, such as
``catalog_token.check_configured``. It never connects to the database.

Prints ``OK`` and exits 0 when valid. Otherwise prints one line per problem,
``NAME: reason``, and exits 1. Only variable names and rules are printed,
never values. Run it in the production Render shell before a release
(docs/deployment.md).
"""

import os
import re
import sys

# A reason like "SCAN_MODEL='bogus' is not an allowed ..." would echo the value.
_QUOTED_VALUE = re.compile(r"=(?:'[^']*'|\"[^\"]*\")")


_TOKEN_SPLIT = re.compile(r"""[\s'"`,;:=()\[\]{}<>]+""")
_MIN_SECRET_FRAGMENT = 4

# Variable names in a message (SCAN_MODEL, ENVIRONMENT) are what the reader
# needs; they are never redacted.
_VARIABLE_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+|ENVIRONMENT")

# Every raw value the check can see (process env and .env files), set by main().
_raw_values: list[str] = []


def _collect_raw_values() -> list[str]:
    values = [v for v in os.environ.values() if v]
    from dotenv import dotenv_values

    # A missing file yields no values; a broken one is reported by Settings.
    values.extend(v for v in dotenv_values(".env").values() if v)
    return values


def _reason(text: str) -> str:
    """First line of ``text`` with anything that looks like a configuration
    value removed: quoted ``=value`` text, and every token of 4+ characters
    that occurs inside a raw environment or .env value."""
    line = _QUOTED_VALUE.sub("=<redacted>", text).splitlines()[0]
    parts = _TOKEN_SPLIT.split(line)
    for token in set(parts):
        if len(token) < _MIN_SECRET_FRAGMENT or _VARIABLE_NAME.fullmatch(token):
            continue
        if any(token in value for value in _raw_values):
            line = line.replace(token, "<redacted>")
    return line


def collect_problems() -> list[str]:
    """Return ``["NAME: reason", ...]``; empty when the environment is valid."""
    # Imported here: importing app.core.config builds the module-level
    # ``settings``, which raises when the environment is invalid.
    from pydantic import ValidationError

    try:
        from app.core.config import (
            BlankSettingError,
            Settings,
            check_turnstile_configured,
        )
    except ValidationError as exc:
        return _validation_problems(exc)
    except ValueError as exc:  # BlankSettingError (not importable here)
        return [f"settings: {_reason(str(exc))}"]

    try:
        cfg = Settings()
    except ValidationError as exc:
        return _validation_problems(exc)
    except BlankSettingError as exc:
        return [f"settings: {_reason(str(exc))}"]

    problems: list[str] = []
    from app.core import catalog_token

    # The database-free checks of app.main._validate_config.
    try:
        check_turnstile_configured(cfg)
    except RuntimeError as exc:
        problems.append(f"TURNSTILE_SECRET_KEY: {_reason(str(exc))}")

    try:
        catalog_token.check_configured(cfg)
    except catalog_token.CatalogTokenConfigError as exc:
        problems.append(f"JWT_PRIVATE_KEY: {_reason(str(exc))}")
    return problems


def _validation_problems(exc) -> list[str]:
    problems = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = [str(part) for part in err["loc"]]
        name = loc[0].upper() if loc else "settings"
        msg = err["msg"].removeprefix("Value error, ")
        problems.append(f"{name}: {_reason(msg)}")
    return problems


def main() -> int:
    _raw_values[:] = _collect_raw_values()
    problems = collect_problems()
    if not problems:
        print("OK")
        return 0
    print("Invalid configuration:")
    for line in problems:
        print(f"  {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

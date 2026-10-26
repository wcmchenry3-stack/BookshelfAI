"""``python -m app.core.config_check``: validate the environment like boot does.

Builds ``Settings`` (reading the process environment and ``.env``, exactly as
the app does at import) and runs the checks that need no database, such as
``catalog_token.check_configured``. It never connects to the database.

Prints ``OK`` and exits 0 when valid. Otherwise prints one line per problem,
``NAME: reason``, and exits 1. Only variable names and rules are printed,
never values. Run it in the production Render shell before a release
(docs/deployment.md).
"""

import re
import sys

# A reason like "SCAN_MODEL='bogus' is not an allowed ..." would echo the value.
_QUOTED_VALUE = re.compile(r"=(?:'[^']*'|\"[^\"]*\")")


def _reason(text: str) -> str:
    return _QUOTED_VALUE.sub("=<redacted>", text).splitlines()[0]


def collect_problems() -> list[str]:
    """Return ``["NAME: reason", ...]``; empty when the environment is valid."""
    # Imported here: importing app.core.config builds the module-level
    # ``settings``, which raises when the environment is invalid.
    from pydantic import ValidationError

    try:
        from app.core.config import BlankSettingError, Settings
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

"""The operator CLI: ``python -m app.ai.settings list | get | set | switch``."""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.ai import settings as ai_settings
from app.ai.settings.errors import AISettingValueError
from app.models.ai_settings import AISetting


def parse_cli_value(text: str) -> Any:
    """JSON when it parses (``true``, ``40000000``), else the plain string."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _printable(text: str) -> str:
    """``text`` with every non-printable character (escape sequences,
    newlines) replaced by ``?``, so a hand-edited row cannot drive the
    operator's terminal."""
    return "".join(c if c.isprintable() else "?" for c in text)


def _describe(key: str, stored: Mapping[str, AISetting]) -> str:
    row = stored.get(key)
    if row is None:
        carried = ai_settings.carried_over(
            {k: r.value for k, r in stored.items()},
            {k: r.updated_by for k, r in stored.items()},
        )
        if key in carried:
            return (
                f"{key} = {json.dumps(carried[key])}  (no row; carried over "
                "from the retired quota.<tier>.daily row)"
            )
        value = ai_settings.DEFINITIONS[key].default()
        return f"{key} = {json.dumps(value)}  (no row; env/registry default)"
    current = ai_settings._sanitise_rows({k: r.value for k, r in stored.items()})
    if key not in current:
        value = ai_settings.DEFINITIONS[key].default()
        return (
            f"{key} = {json.dumps(value)}  (stored value is INVALID and ignored; "
            "env/registry default in use)"
        )
    # json.dumps escapes control characters, so values print safely.
    try:
        ai_settings.DEFINITIONS[key].validate(row.value)
    except AISettingValueError:
        return f"{key} = false  (stored value is INVALID; treated as disabled)"
    updated_by = ai_settings._printable(row.updated_by or "?")
    return (
        f"{key} = {json.dumps(row.value)}  "
        f"(updated {row.updated_at:%Y-%m-%d %H:%M:%S %Z} by {updated_by})"
    )


def _default_and_bounds(key: str) -> str:
    definition = ai_settings.DEFINITIONS[key]
    return f"default {json.dumps(definition.default())}; bounds {definition.bounds}"


async def _run(args: argparse.Namespace) -> int:
    async with ai_settings._open_session() as db:
        if args.command == "set":
            updated_by = (
                args.by or os.environ.get("USER") or ai_settings.DEFAULT_CLI_ACTOR
            )
            typed = await ai_settings.set_setting(
                db,
                args.key,
                ai_settings.parse_cli_value(args.value),
                updated_by=updated_by,
            )
            print(
                f"{args.key} = {json.dumps(typed)}  (takes effect within "
                f"{ai_settings.settings.ai_settings_cache_ttl_seconds}s in every process)"
            )
            return 0
        if args.command == "switch":
            updated_by = (
                args.by or os.environ.get("USER") or ai_settings.DEFAULT_CLI_ACTOR
            )
            provider, model = await ai_settings.switch_provider(
                db, args.feature, args.provider, args.model, updated_by=updated_by
            )
            print(
                f"{args.feature}.provider = {json.dumps(provider)}, "
                f"{args.feature}.model = {json.dumps(model)}  (takes effect "
                f"within {ai_settings.settings.ai_settings_cache_ttl_seconds}s "
                "in every process)"
            )
            return 0
        stored = await ai_settings.read_rows(db)
        if args.command == "list":
            for area, keys in ai_settings.keys_by_area().items():
                print(f"\n== {area} ==")
                for key in keys:
                    print(
                        f"{ai_settings._describe(key, stored)}  "
                        f"[{ai_settings._default_and_bounds(key)}]"
                    )
            return 0
        print(ai_settings._describe(args.key, stored))
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.ai.settings",
        description="Read or change the ai_settings dials and kill switches.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    set_cmd = sub.add_parser("set", help="validate and store one value")
    set_cmd.add_argument("key", choices=ai_settings.KNOWN_KEYS)
    set_cmd.add_argument("value", help="JSON (true, 40000000) or a plain string")
    set_cmd.add_argument("--by", help="who made the change (default: $USER)")
    get_cmd = sub.add_parser("get", help="show one key")
    get_cmd.add_argument("key", choices=ai_settings.KNOWN_KEYS)
    sub.add_parser("list", help="show every key by area, with default and bounds")
    switch_cmd = sub.add_parser(
        "switch", help="set a feature's provider and model together"
    )
    switch_cmd.add_argument("feature", choices=ai_settings.SCAN_FEATURES)
    switch_cmd.add_argument("provider", choices=sorted(ai_settings.ALLOWED_MODELS))
    switch_cmd.add_argument(
        "--model", help="default: the registry's model for that provider"
    )
    switch_cmd.add_argument("--by", help="who made the change (default: $USER)")
    args = parser.parse_args(argv)

    # Fail fast on a bad value before touching the database.
    try:
        if args.command == "set":
            ai_settings.validate_value(
                args.key, ai_settings.parse_cli_value(args.value)
            )
        elif args.command == "switch":
            ai_settings.validate_switch(args.feature, args.provider, args.model)
    except AISettingValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    async def _run_and_dispose() -> int:
        try:
            return await ai_settings._run(args)
        finally:
            await ai_settings._dispose_engine()

    try:
        return asyncio.run(_run_and_dispose())
    except AISettingValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.command == "set" and args.key.endswith(".provider"):
            print(
                "hint: use `switch <feature> <provider>` to change a "
                "provider and its model together",
                file=sys.stderr,
            )
        return 2
    except (SQLAlchemyError, OSError) as exc:
        print(
            f"error: could not use the database ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1

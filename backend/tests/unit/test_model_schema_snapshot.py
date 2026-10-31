"""The models' schema is pinned (#814).

``tests/fixtures/model_schema_snapshot.json`` holds every table of
``Base.metadata``: the columns in order with their type, nullability, keys,
index and unique flags, client and server defaults, ``onupdate`` and foreign
keys, plus the table's indexes and named constraints. It was generated from
the models before the column mixins (``app/models/_mixins.py``) replaced the
repeated column definitions, so this test shows the mixins changed nothing.

A real schema change goes with a migration; regenerate the snapshot in the
same PR and review its diff:

    cd backend && python -m tests.unit.test_model_schema_snapshot --write
"""

import json
import sys
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.database import Base

SNAPSHOT = (
    Path(__file__).resolve().parents[1] / "fixtures" / "model_schema_snapshot.json"
)
_PG = postgresql.dialect()


def _default(default) -> str | None:
    if default is None:
        return None
    arg = default.arg
    if hasattr(arg, "compile"):
        return f"sql:{arg.compile(dialect=_PG)}"
    if callable(arg):
        fn = getattr(arg, "__wrapped__", arg)
        return f"callable:{fn.__module__}.{fn.__qualname__}"
    return f"value:{arg!r}"


def _column(column) -> dict:
    return {
        "name": column.name,
        "type": str(column.type.compile(dialect=_PG)),
        "nullable": column.nullable,
        "primary_key": column.primary_key,
        "index": column.index,
        "unique": column.unique,
        "default": _default(column.default),
        "server_default": _default(column.server_default),
        "onupdate": _default(column.onupdate),
        "foreign_keys": sorted(
            f"{fk.target_fullname} ondelete={fk.ondelete}" for fk in column.foreign_keys
        ),
    }


def schema() -> dict:
    """Every table's columns (in order), indexes and named constraints."""
    return {
        name: {
            "columns": [_column(c) for c in table.columns],
            "indexes": sorted(
                str(CreateIndex(i).compile(dialect=_PG)).strip() for i in table.indexes
            ),
            "constraints": sorted(
                f"{type(c).__name__}:{c.name}"
                for c in table.constraints
                if isinstance(c.name, str)
            ),
        }
        for name, table in sorted(Base.metadata.tables.items())
    }


def test_tables_match_the_snapshot():
    assert sorted(schema()) == sorted(json.loads(SNAPSHOT.read_text()))


def test_columns_indexes_and_constraints_match_the_snapshot():
    """Table by table, so a failure names the table that changed."""
    pinned = json.loads(SNAPSHOT.read_text())
    for name, table in schema().items():
        assert table == pinned[name], name


def test_column_order_matches_the_snapshot():
    """Mixin columns keep their place (``sort_order``): ``id`` and
    ``user_id`` first, ``created_at`` and ``updated_at`` last."""
    pinned = json.loads(SNAPSHOT.read_text())
    for name, table in schema().items():
        assert [c["name"] for c in table["columns"]] == [
            c["name"] for c in pinned[name]["columns"]
        ], name


def _dump(data: dict) -> str:
    """``data`` as JSON with one column, index or constraint per line."""

    def block(key: str, rows: list) -> str:
        inner = ",\n".join(f"   {json.dumps(r)}" for r in rows)
        return f'  "{key}": [\n{inner}\n  ]' if rows else f'  "{key}": []'

    tables = [
        f" {json.dumps(name)}: {{\n"
        + ",\n".join(block(k, t[k]) for k in ("columns", "indexes", "constraints"))
        + "\n }"
        for name, t in data.items()
    ]
    return "{\n" + ",\n".join(tables) + "\n}\n"


if __name__ == "__main__":
    if sys.argv[1:] != ["--write"]:
        sys.exit("usage: python -m tests.unit.test_model_schema_snapshot --write")
    SNAPSHOT.write_text(_dump(schema()))
    print(f"wrote {SNAPSHOT}")

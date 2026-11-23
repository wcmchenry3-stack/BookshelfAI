"""Drift guard for docs/DATA-INVENTORY.md (#1011).

The inventory lists every table of the ``public`` schema. Its first column is
the table name in backticks; the set of names must equal the ORM tables plus
``alembic_version`` (owned by Alembic, not by a model).
"""

import re
from pathlib import Path

import pytest

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.database import Base
from app.services.account_export import EXCLUDED_TABLES, SECTIONS
from tests._user_tables import user_owned_tables

DOC = Path(__file__).resolve().parents[3] / "docs" / "DATA-INVENTORY.md"
NON_MODEL_TABLES = {"alembic_version"}
HOW_TO_FIX = (
    "add or fix its row in the table of docs/DATA-INVENTORY.md "
    "(what it holds, personal data, retention, export, deletion, trash purge, bytes)"
)
HEADER = [
    "table",
    "holds",
    "personal data",
    "retention",
    "export",
    "deletion",
    "trash purge",
    "bytes per row",
    "bytes per active user",
]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_inventory(text: str) -> dict[str, list[str]]:
    """Rows of the table whose first cell is a backticked table name."""
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = re.match(r"^\|\s*`([a-z_0-9]+)`\s*\|", line)
        if match:
            name = match.group(1)
            assert (
                name not in rows
            ), f"table {name!r} is listed twice in DATA-INVENTORY.md"
            rows[name] = _cells(line)
    return rows


@pytest.fixture(scope="module")
def inventory() -> dict[str, list[str]]:
    assert DOC.is_file(), f"{DOC} is missing"
    return parse_inventory(DOC.read_text(encoding="utf-8"))


def test_parser_reads_a_row():
    rows = parse_inventory("| `a_table` | x | Yes |\n| not a row | y |\n|---|---|\n")
    assert rows == {"a_table": ["`a_table`", "x", "Yes"]}


def test_inventory_lists_exactly_the_schema_tables(inventory):
    expected = set(Base.metadata.tables) | NON_MODEL_TABLES
    missing = sorted(expected - set(inventory))
    unknown = sorted(set(inventory) - expected)
    assert (
        not missing
    ), f"tables in the schema but not in docs/DATA-INVENTORY.md: {missing}: {HOW_TO_FIX}"
    assert not unknown, (
        f"tables in docs/DATA-INVENTORY.md that are not in the schema: {unknown}: "
        "remove the row (or the table was renamed: fix its name)"
    )


def test_every_row_has_every_column(inventory):
    for name, cells in inventory.items():
        assert len(cells) == len(HEADER), (
            f"row {name!r} has {len(cells)} cells, expected {len(HEADER)} "
            f"({', '.join(HEADER)}): {HOW_TO_FIX}"
        )
        assert all(cells), f"row {name!r} has an empty cell: {HOW_TO_FIX}"


def test_user_owned_tables_are_marked_personal_data(inventory):
    for name in sorted(user_owned_tables()):
        personal = inventory[name][2]
        assert personal.startswith("Yes"), (
            f"{name!r} is owned by a user but the inventory says personal data is "
            f"{personal!r}: {HOW_TO_FIX}"
        )


def test_export_column_matches_the_export_service(inventory):
    def tag(name: str) -> str:
        return inventory[name][4].strip("` ").split(" ")[0].strip("`")

    sections = {n for n in inventory if tag(n) == "section"}
    excluded = {n for n in inventory if tag(n) == "excluded"}
    assert sections == set(SECTIONS), (
        f"Export column says `section` for {sorted(sections)} but SECTIONS in "
        f"account_export.py has {sorted(SECTIONS)}: {HOW_TO_FIX}"
    )
    assert excluded == set(EXCLUDED_TABLES), (
        f"Export column says `excluded` for {sorted(excluded)} but EXCLUDED_TABLES "
        f"has {sorted(EXCLUDED_TABLES)}: {HOW_TO_FIX}"
    )
    allowed = {"section", "excluded", "catalog", "n/a"}
    for name in inventory:
        assert (
            tag(name) in allowed
        ), f"{name!r}: Export column {inventory[name][4]!r} unknown"

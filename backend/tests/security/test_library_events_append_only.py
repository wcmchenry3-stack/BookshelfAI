"""The library audit log is append-only in code (#684).

``library_events`` rows are inserted by ``app/services/library_events.py``
and deleted only by the retention job,
``app/services/library_events_retention.py``. This test reads every module
under ``backend/app`` and fails if:

- any module other than those two (and the model package) imports
  ``LibraryEvent``, so no other code can load, change or delete an event
  through the ORM;
- the writer module calls ``update(...)`` or ``delete(...)`` on
  ``LibraryEvent``, calls ``.update`` / ``.delete`` on a
  ``query(LibraryEvent)``, or assigns to an attribute of an object (an
  ORM update);
- any module except the retention job does any of that, or holds SQL text
  that updates or deletes ``library_events``.

The database enforces the same for ``UPDATE`` (migration 0030 revokes it
from the app role; ``tests/integration/test_library_events_db.py``).

Run on its own with:
``pytest -m security tests/security/test_library_events_append_only.py``
"""

import ast
import re
from pathlib import Path

import pytest

import app as app_package

pytestmark = pytest.mark.security

APP_ROOT = Path(app_package.__file__).resolve().parent
WRITER = Path("services") / "library_events.py"
RETENTION = Path("services") / "library_events_retention.py"
# Modules allowed to import LibraryEvent at all.
IMPORTERS = frozenset(
    {
        Path("models") / "library_event.py",
        Path("models") / "__init__.py",
        WRITER,
        RETENTION,
    }
)
MODEL = "LibraryEvent"
TABLE = "library_events"
RAW_SQL_RE = re.compile(
    rf"\b(UPDATE\s+(ONLY\s+)?|DELETE\s+FROM\s+(ONLY\s+)?)(public\.)?\"?{TABLE}\b",
    re.IGNORECASE,
)


def _names_model(node: ast.AST) -> bool:
    """``LibraryEvent``, ``x.LibraryEvent``, ``LibraryEvent.__table__``, the
    table name as a string, or a call / subscript built from one."""
    if isinstance(node, ast.Name):
        return node.id == MODEL
    if isinstance(node, ast.Attribute):
        return node.attr == MODEL or _names_model(node.value)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value == TABLE
    # ``sa.table("library_events")``, ``Table("library_events", ...)``.
    if isinstance(node, ast.Call):
        return any(_names_model(a) for a in node.args)
    # ``metadata.tables["library_events"]``.
    if isinstance(node, ast.Subscript):
        return _names_model(node.slice) or _names_model(node.value)
    return False


def _callee(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_query_of_model(node: ast.AST) -> bool:
    """Somewhere in a call chain is ``query(LibraryEvent)``."""
    while isinstance(node, ast.Call | ast.Attribute):
        if (
            isinstance(node, ast.Call)
            and _callee(node) == "query"
            and any(_names_model(a) for a in node.args)
        ):
            return True
        node = node.func if isinstance(node, ast.Call) else node.value
    return False


def write_violations(source: str, path: Path) -> list[str]:
    """Every UPDATE / DELETE of library_events the source expresses."""
    found = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _callee(node)
            if name in {"update", "delete"} and any(_names_model(a) for a in node.args):
                found.append(f"{path}:{node.lineno}: {name}({MODEL})")
            if (
                name in {"update", "delete"}
                and isinstance(node.func, ast.Attribute)
                and _is_query_of_model(node.func.value)
            ):
                found.append(f"{path}:{node.lineno}: query({MODEL}).{name}()")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and RAW_SQL_RE.search(node.value)
        ):
            found.append(f"{path}:{node.lineno}: SQL text writes {TABLE}")
    return found


def import_violations(source: str, path: Path) -> list[str]:
    # Constants (the event types) may be imported; the class, the module
    # object or ``*`` may not.
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            bad = any(a.name in {MODEL, "library_event", "*"} for a in node.names)
        elif isinstance(node, ast.Import):
            bad = any(a.name.endswith("models.library_event") for a in node.names)
        else:
            continue
        if bad:
            found.append(f"{path}:{node.lineno}: imports {MODEL}")
    return found


def attribute_assignments(source: str, path: Path) -> list[str]:
    """Any ``obj.attr = ...``: in the writer, that could only update an
    event loaded or added earlier."""
    found = []
    for node in ast.walk(ast.parse(source)):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute):
                found.append(f"{path}:{node.lineno}: assigns .{target.attr}")
    return found


def _modules():
    for file in sorted(APP_ROOT.rglob("*.py")):
        yield file.relative_to(APP_ROOT), file.read_text(encoding="utf-8")


def test_scan_covers_the_app_and_the_two_allowed_modules():
    paths = {p for p, _ in _modules()}
    assert WRITER in paths and RETENTION in paths
    assert len(paths) > 50, "the scan read too few modules"


def test_no_update_or_delete_of_library_events_outside_retention():
    problems = []
    for path, source in _modules():
        if path == RETENTION:
            continue
        problems += write_violations(source, path)
        if path not in IMPORTERS:
            problems += import_violations(source, path)
    assert problems == [], "\n".join(problems)


def test_the_writer_never_changes_an_event_it_added():
    path = WRITER
    source = (APP_ROOT / path).read_text(encoding="utf-8")
    assert attribute_assignments(source, path) == []


def test_the_retention_job_is_the_one_delete():
    """The allowlisted module really is where the delete lives (so the
    scanner sees it), and it never updates."""
    source = (APP_ROOT / RETENTION).read_text(encoding="utf-8")
    found = write_violations(source, RETENTION)
    assert found and all("delete" in f for f in found), found


@pytest.mark.parametrize(
    "snippet",
    [
        "stmt = update(LibraryEvent).values(event_type='x')",
        "stmt = delete(LibraryEvent)",
        "stmt = sa.delete(models.LibraryEvent)",
        "stmt = delete(LibraryEvent.__table__)",
        "db.query(LibraryEvent).filter(x).delete()",
        "db.query(LibraryEvent).update({'event_type': 'x'})",
        "db.execute(text('UPDATE library_events SET event_type = 1'))",
        "db.execute(text('delete from public.library_events'))",
        "db.execute(text('DELETE FROM \"library_events\" WHERE id = 1'))",
        "stmt = update('library_events')",
        "stmt = delete(sa.table('library_events'))",
        "stmt = update(Table('library_events', metadata))",
        "stmt = delete(Base.metadata.tables['library_events'])",
    ],
)
def test_scanner_catches_planted_writes(snippet):
    assert write_violations(snippet, Path("planted.py")) != []


@pytest.mark.parametrize(
    "snippet",
    [
        "stmt = insert(LibraryEvent)",
        "stmt = select(LibraryEvent.id)",
        "stmt = delete(UserBook)",
        "stmt = delete(metadata.tables['user_books'])",
        "stmt = insert(sa.table('library_events'))",
        "x = 'ON DELETE SET NULL on library_events'",
    ],
)
def test_scanner_allows_inserts_reads_and_other_tables(snippet):
    assert write_violations(snippet, Path("planted.py")) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "from app.models.library_event import LibraryEvent",
        "from app.models import LibraryEvent",
        "import app.models.library_event",
        "from app.models import library_event",
        "from app.models.library_event import *",
    ],
)
def test_scanner_catches_other_importers(snippet):
    assert import_violations(snippet, Path("planted.py")) != []


def test_scanner_allows_importing_the_event_types():
    snippet = "from app.models.library_event import EVENT_DELETED"
    assert import_violations(snippet, Path("planted.py")) == []


def test_scanner_catches_an_attribute_assignment():
    assert attribute_assignments("event.event_type = 'x'", Path("p.py")) != []

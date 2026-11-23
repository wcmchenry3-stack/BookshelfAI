"""Which tables hold a user's data, read from the models (#694, for #291).

:func:`user_owned_tables` is every table of ``Base.metadata`` with a foreign
key to ``users``, plus every table with a foreign key to one of those
(``scan_corrections`` reaches its user through ``scan_records``), and
``users`` itself. It is computed, not listed, so a new table with a user
column shows up here without anyone remembering to add it.

Used by the account export's enumeration test
(``tests/unit/test_account_export_document.py``): every such table must be
exported or excluded with a reason. Account deletion (#291) should check
its own coverage against the same set.
"""

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.database import Base

USERS = "users"


def _references(table) -> set[str]:
    return {fk.column.table.name for fk in table.foreign_keys}


def user_owned_tables(metadata=Base.metadata) -> set[str]:
    owned = {USERS}
    changed = True
    while changed:
        changed = False
        for name, table in metadata.tables.items():
            if name not in owned and _references(table) & owned:
                owned.add(name)
                changed = True
    return owned

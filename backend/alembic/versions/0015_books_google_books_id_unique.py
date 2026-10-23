"""books.google_books_id unique (partial): merge duplicates, then index (#613)

Before this revision ``books.google_books_id`` had no unique constraint, so
two concurrent first adds of a book known only to Google Books (no Open
Library work ID) could create two ``books`` rows (#611 made every lookup
tolerate that). This revision merges any duplicates that exist and then adds
a partial unique index, ``uq_books_google_books_id`` on
``books (google_books_id) WHERE google_books_id IS NOT NULL``, so no new
duplicate can be created. ``WishlistService`` re-reads and links the winner's
row when its insert loses the race on this index.

Merge (owner decision on #613, option A). For each ``google_books_id`` with
more than one row, the oldest row (``created_at``, then ``id``) is kept and
every newer duplicate is merged into it:

1. ``user_books`` (``uq_user_books_user_book``: one entry per user and
   book; the table has no ``deleted_at``). Per user, the entry already on the
   kept book survives; if there is none, the user's oldest entry on a
   duplicate (``created_at``, then ``id``) survives. Every field of the
   survivor that is NULL (``edition_id``, ``notes``, ``rating`` and the
   ``*_at`` timestamps) takes the first non-NULL value from the user's other
   entries (oldest first). ``status`` becomes the most advanced status among
   all of the user's merged entries (wishlisted < purchased < reading <
   read). The other entries are then deleted (hard delete: their content is
   now on the survivor) and the survivor is pointed at the kept book.
2. ``editions`` of the duplicates are pointed at the kept book. The only
   other unique constraint there is the global ``isbn_13``, which a book_id
   change cannot break.
3. If the kept book has no ``open_library_work_id`` and a duplicate has one,
   the oldest duplicate's value moves to the kept book (after the duplicates
   are deleted, as ``open_library_work_id`` is unique), so a provider ID is
   not lost from the catalog.
4. The duplicate ``books`` rows are deleted. ``user_books`` and ``editions``
   are the only tables that reference ``books.id`` (both ``ON DELETE
   RESTRICT``, 0004), so nothing is orphaned; scan records never hold a book
   ID.

Counts are logged (``alembic`` logger), with at most ``_LOG_IDS`` of each
batch's deleted book IDs. Duplicates are processed in batches of
``_BATCH_GROUPS`` kept books, with set-based statements (no per-row Python
loop). The batches bound the size of each statement only: everything runs in
one transaction, so they do not bound how long the locks are held or the WAL
written. Pre-launch there are expected to be none; more than
``_MAX_DUPLICATES`` duplicate rows fails the upgrade (nothing is changed)
with a message to run the merge by hand, rather than hold the locks below
for a long deploy-time transaction.

Locking and why not ``CREATE INDEX CONCURRENTLY``: before the duplicates are
read, ``LOCK TABLE books, user_books, editions IN SHARE ROW EXCLUSIVE MODE``
(under a 5 s ``lock_timeout``) blocks writes to all three tables; reads still
run. So no request can, during the merge, update a redundant entry after its
fields were copied (lost update), add a ``user_books`` row or edition on a
duplicate (the ``books`` DELETE would then hit the RESTRICT FK and abort),
delete a survivor (losing the merged data) or insert a new duplicate before
the index is built; and either everything commits or nothing does.
``env.py`` runs every pending revision in one transaction, so these locks are
held until the whole ``alembic upgrade`` run commits; ``lock_timeout`` is
reset to the default at the end of this revision so later revisions are not
affected.
``CONCURRENTLY`` cannot run inside a transaction: it would need an autocommit
block after the merge has committed, leaving a window for a new duplicate
(the build would then fail and leave an INVALID index for a manual cleanup
before the Render ``preDeployCommand: alembic upgrade head`` could be
retried). ``books`` is small, so a plain build holds the write lock for
milliseconds; revisit with ``CONCURRENTLY`` if the table grows large.

Offline mode (``alembic upgrade --sql``) is not supported: the merge reads
the duplicates before writing, so 0015 raises instead of emitting SQL (a
script without the merge would fail on the index wherever duplicates exist).

Downgrade drops the index only. Merged rows are not restored: the deleted
duplicate ``books`` rows and redundant ``user_books`` entries are gone, and
the re-pointed rows keep pointing at the kept book.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-23
"""

import logging

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

INDEX = "uq_books_google_books_id"
_LOCK_TIMEOUT = "5s"
# Kept books (each with all of its duplicates) merged per batch.
_BATCH_GROUPS = 500
# Deleted book IDs logged per batch (the count is always logged).
_LOG_IDS = 20
# Duplicate rows above which the upgrade refuses: ~0 are expected pre-launch,
# and the merge holds write locks on books, user_books and editions until the
# whole run commits, so a large merge belongs in a planned manual run, not a
# deploy's preDeployCommand.
_MAX_DUPLICATES = 10_000
_LOCKED_TABLES = "books, user_books, editions"
# Nullable user_books fields a redundant entry can fill on the survivor.
_MERGED_FIELDS = (
    "edition_id",
    "notes",
    "rating",
    "wishlisted_at",
    "purchased_at",
    "started_at",
    "finished_at",
)
# Frozen copy of app.models.user_book.VALID_STATUSES at this revision, in
# order of progress: a merged entry keeps the most advanced one.
_STATUS_ORDER = ("wishlisted", "purchased", "reading", "read")
_STATUS_RANK = (
    "CASE r.status "
    + " ".join(f"WHEN '{st}' THEN {i}" for i, st in enumerate(_STATUS_ORDER))
    + " ELSE -1 END"
)

log = logging.getLogger("alembic.runtime.migration")


def _exec(conn, sql: str, **params):
    return conn.execute(sa.text(sql), params)


def _merge_batch(conn) -> tuple[int, int, int, list]:
    """Merge the duplicates in ``pg_temp._gb_batch`` (dup_id, keep_id) into their
    kept books. Returns (user_books deleted, user_books re-pointed,
    editions re-pointed, deleted book IDs)."""
    # Every entry on a kept book or one of its duplicates; rn 1 survives
    # per (user, kept book): the entry on the kept book, else the oldest.
    _exec(
        conn,
        """
        CREATE TEMP TABLE pg_temp._gb_ub AS
        SELECT ub.id, ub.user_id, g.keep_id,
               row_number() OVER (
                   PARTITION BY ub.user_id, g.keep_id
                   ORDER BY (ub.book_id = g.keep_id) DESC, ub.created_at, ub.id
               ) AS rn
        FROM user_books ub
        JOIN (
            SELECT keep_id AS book_id, keep_id FROM pg_temp._gb_batch
            UNION
            SELECT dup_id AS book_id, keep_id FROM pg_temp._gb_batch
        ) g ON g.book_id = ub.book_id
        """,
    )
    fills = ",\n".join(f"""{f} = COALESCE(s.{f}, (
                SELECT r.{f} FROM user_books r
                JOIN pg_temp._gb_ub o ON o.id = r.id
                WHERE o.user_id = x.user_id AND o.keep_id = x.keep_id
                  AND o.rn > 1 AND r.{f} IS NOT NULL
                ORDER BY o.rn LIMIT 1))""" for f in _MERGED_FIELDS)
    _exec(
        conn,
        f"""
        UPDATE user_books s SET {fills},
            status = (
                SELECT r.status FROM user_books r
                JOIN pg_temp._gb_ub o ON o.id = r.id
                WHERE o.user_id = x.user_id AND o.keep_id = x.keep_id
                ORDER BY {_STATUS_RANK} DESC, o.rn LIMIT 1),
            updated_at = now()
        FROM pg_temp._gb_ub x
        WHERE x.id = s.id AND x.rn = 1
          AND EXISTS (
              SELECT 1 FROM pg_temp._gb_ub o
              WHERE o.user_id = x.user_id AND o.keep_id = x.keep_id AND o.rn > 1
          )
        """,
    )
    ub_deleted = _exec(
        conn,
        "DELETE FROM user_books WHERE id IN (SELECT id FROM pg_temp._gb_ub WHERE rn > 1)",
    ).rowcount
    ub_moved = _exec(
        conn,
        """
        UPDATE user_books ub SET book_id = x.keep_id, updated_at = now()
        FROM pg_temp._gb_ub x
        WHERE x.id = ub.id AND x.rn = 1 AND ub.book_id <> x.keep_id
        """,
    ).rowcount
    _exec(conn, "DROP TABLE pg_temp._gb_ub")

    editions_moved = _exec(
        conn,
        """
        UPDATE editions e SET book_id = b.keep_id
        FROM pg_temp._gb_batch b WHERE e.book_id = b.dup_id
        """,
    ).rowcount

    # The oldest duplicate's Open Library work ID, for kept books with none.
    _exec(
        conn,
        """
        CREATE TEMP TABLE pg_temp._gb_ol AS
        SELECT DISTINCT ON (b.keep_id) b.keep_id, d.open_library_work_id
        FROM pg_temp._gb_batch b
        JOIN books d ON d.id = b.dup_id
        JOIN books k ON k.id = b.keep_id
        WHERE k.open_library_work_id IS NULL
          AND d.open_library_work_id IS NOT NULL
        ORDER BY b.keep_id, d.created_at, d.id
        """,
    )
    deleted = [
        row[0]
        for row in _exec(
            conn,
            "DELETE FROM books WHERE id IN (SELECT dup_id FROM pg_temp._gb_batch) "
            "RETURNING id",
        )
    ]
    _exec(
        conn,
        """
        UPDATE books k SET open_library_work_id = o.open_library_work_id
        FROM pg_temp._gb_ol o WHERE k.id = o.keep_id
        """,
    )
    _exec(conn, "DROP TABLE pg_temp._gb_ol")
    return ub_deleted, ub_moved, editions_moved, deleted


def merge_duplicates(conn) -> dict[str, int]:
    """Merge every duplicate ``google_books_id`` into its oldest row; the
    totals are returned (and logged)."""
    _exec(conn, "DROP TABLE IF EXISTS pg_temp._gb_dupes")
    _exec(
        conn,
        """
        CREATE TEMP TABLE pg_temp._gb_dupes AS
        SELECT id AS dup_id, keep_id FROM (
            SELECT id, first_value(id) OVER (
                PARTITION BY google_books_id ORDER BY created_at, id
            ) AS keep_id
            FROM books WHERE google_books_id IS NOT NULL
        ) ranked
        WHERE id <> keep_id
        """,
    )
    found = _exec(conn, "SELECT count(*) FROM pg_temp._gb_dupes").scalar()
    if found > _MAX_DUPLICATES:
        raise RuntimeError(
            f"0015: {found} duplicate books rows by google_books_id (limit "
            f"{_MAX_DUPLICATES}); merge them manually (merge_duplicates in this "
            "revision, in a maintenance window) before running the upgrade"
        )
    totals = dict.fromkeys(
        ("books", "user_books_deleted", "user_books_moved", "editions_moved"), 0
    )
    while True:
        _exec(conn, "DROP TABLE IF EXISTS pg_temp._gb_batch")
        _exec(
            conn,
            """
            CREATE TEMP TABLE pg_temp._gb_batch AS
            SELECT dup_id, keep_id FROM pg_temp._gb_dupes
            WHERE keep_id IN (
                SELECT DISTINCT keep_id FROM pg_temp._gb_dupes ORDER BY keep_id LIMIT :n
            )
            """,
            n=_BATCH_GROUPS,
        )
        if not _exec(conn, "SELECT count(*) FROM pg_temp._gb_batch").scalar():
            break
        ub_deleted, ub_moved, editions_moved, deleted = _merge_batch(conn)
        _exec(
            conn,
            "DELETE FROM pg_temp._gb_dupes WHERE dup_id IN (SELECT dup_id FROM pg_temp._gb_batch)",
        )
        totals["books"] += len(deleted)
        totals["user_books_deleted"] += ub_deleted
        totals["user_books_moved"] += ub_moved
        totals["editions_moved"] += editions_moved
        log.info(
            "0015: deleted %d duplicate books in this batch, first %d: %s",
            len(deleted),
            min(len(deleted), _LOG_IDS),
            ", ".join(str(i) for i in deleted[:_LOG_IDS]),
        )
    _exec(conn, "DROP TABLE IF EXISTS pg_temp._gb_batch")
    _exec(conn, "DROP TABLE pg_temp._gb_dupes")
    log.info(
        "0015: merged %(books)d duplicate books; user_books deleted "
        "%(user_books_deleted)d, re-pointed %(user_books_moved)d; editions "
        "re-pointed %(editions_moved)d",
        totals,
    )
    return totals


def upgrade() -> None:
    # The same check as context.is_offline_mode(), but it also works when the
    # revision is run through Operations directly (the migration test).
    if op.get_context().as_sql:
        raise RuntimeError(
            "0015 does not support offline SQL generation (alembic --sql): "
            "its duplicate merge reads rows; run it online"
        )
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    # Blocks writes (not reads) to the three tables until the run commits,
    # taken before the duplicates are read (see the docstring).
    op.execute(f"LOCK TABLE {_LOCKED_TABLES} IN SHARE ROW EXCLUSIVE MODE")
    merge_duplicates(op.get_bind())
    op.create_index(
        INDEX,
        "books",
        ["google_books_id"],
        unique=True,
        postgresql_where=sa.text("google_books_id IS NOT NULL"),
    )
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def downgrade() -> None:
    # The index only: merged rows are not restored (see the docstring).
    op.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
    op.drop_index(INDEX, table_name="books")
    op.execute("SET LOCAL lock_timeout = DEFAULT")

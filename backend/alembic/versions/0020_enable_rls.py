"""Enable row level security on every table, with no policies

Defence in depth for the Supabase Data API. Supabase publishes the ``public``
schema through PostgREST to the ``anon`` and ``authenticated`` roles. The Data
API is switched off for prod and the dev branch (docs/RENDER.md), but with RLS
off a single dashboard toggle would expose every row, including ``users`` and
``refresh_tokens``, to anyone holding the public anon key. Supabase's security
advisor reports that as critical.

With RLS on and no policies, ``anon`` and ``authenticated`` see no rows and
can't write any. The app is unaffected: it connects as ``postgres``, which
owns every table and has ``BYPASSRLS``, and table owners skip RLS anyway
unless it is FORCEd (it isn't). The nightly ``pg_dump`` (db-backup.yml) uses
the same role, so ``row_security = off`` doesn't make it fail.

Every table created after this one must enable RLS in its own migration;
``tests/integration/test_rls_enabled_db.py`` fails the build otherwise.

Downgrade turns RLS back off. No data is touched either way.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-26
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

# Every table in the public schema at revision 0019.
TABLES = (
    "alembic_version",
    "ai_enhanced_zero_results",
    "ai_inflight",
    "ai_quota",
    "ai_settings",
    "ai_spend_daily",
    "ai_usage",
    "ai_usage_daily",
    "books",
    "editions",
    "refresh_tokens",
    "scan_corrections",
    "scan_corrections_daily",
    "scan_records",
    "user_books",
    "user_custom_books",
    "users",
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

"""Device attestation — device_keys table (#753)

Expand only:

- ``device_key_platform`` enum ('ios', 'android'): the attestation platform.
- ``device_keys`` table: one row per attested device key per user.

  Columns:
    id              UUID PK (uuid4, client-generated)
    user_id         UUID FK -> users.id ON DELETE CASCADE, indexed
    platform        device_key_platform
    key_id          VARCHAR(256) — App Attest key ID (iOS) or device ID (Android)
    public_key_cbor BYTEA — COSE-encoded EC public key from attestation cert (iOS);
                    empty bytes for Android rows (no persistent key stored)
    counter         BIGINT — App Attest assertion counter, prevents replay (0 for Android)
    last_used_at    TIMESTAMPTZ nullable — updated on every verified assertion
    created_at      TIMESTAMPTZ server default NOW()

  Constraints:
    pk_device_keys on (id)
    uq_device_keys_user_key on (user_id, key_id) — one key_id per user
    ix_device_keys_user_id on (user_id) — ownership query
    ix_device_keys_key_id on (key_id) — registration uniqueness check

The previous release does not read or write this table; downgrade drops it
and the enum cleanly.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE device_key_platform AS ENUM ('ios', 'android')"
    )
    op.create_table(
        "device_keys",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "platform",
            sa.Enum("ios", "android", name="device_key_platform", create_type=False),
            nullable=False,
        ),
        sa.Column("key_id", sa.String(256), nullable=False),
        sa.Column("public_key_cbor", sa.LargeBinary, nullable=False),
        sa.Column(
            "counter",
            sa.BigInteger,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "key_id", name="uq_device_keys_user_key"),
    )
    op.create_index("ix_device_keys_user_id", "device_keys", ["user_id"])
    op.create_index("ix_device_keys_key_id", "device_keys", ["key_id"])


def downgrade() -> None:
    op.drop_index("ix_device_keys_key_id", table_name="device_keys")
    op.drop_index("ix_device_keys_user_id", table_name="device_keys")
    op.drop_table("device_keys")
    op.execute("DROP TYPE device_key_platform")

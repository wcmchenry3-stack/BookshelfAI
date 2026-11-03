from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._mixins import CreatedAtMixin, IdMixin, UserOwnedMixin

_PLATFORM_ENUM = Enum("ios", "android", name="device_key_platform")


class DeviceKey(IdMixin, UserOwnedMixin, CreatedAtMixin, Base):
    """An attested device key registered by a user (#753).

    iOS: stores the App Attest COSE public key and assertion counter.
    Android: no persistent key (Play Integrity tokens verified per-request).
    """

    __tablename__ = "device_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "key_id", name="uq_device_keys_user_key"),
    )
    user_id_indexed = True

    platform: Mapped[str] = mapped_column(_PLATFORM_ENUM, nullable=False)
    # App Attest key ID (iOS) or device ID (Android) — max 256 chars.
    key_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    # COSE-encoded EC public key from the attestation certificate (iOS only).
    # Android rows store an empty bytes value (they carry no persistent key).
    public_key_cbor: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # App Attest assertion counter — monotonic, prevents assertion replay.
    # Always 0 for Android (Play Integrity has no counter).
    counter: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

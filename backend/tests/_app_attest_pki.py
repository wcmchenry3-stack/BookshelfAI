"""A throwaway App Attest PKI for tests (#1051).

Builds a root, an intermediate and a leaf shaped like Apple's (P-384 CAs, a
P-256 leaf carrying the nonce extension 1.2.840.113635.100.8.2), and the CBOR
attestation and assertion objects a device would send. Tests hand the root to
``LiveAppleAttestVerifier(root_ca_pem=...)``; production always uses Apple's
embedded root. Each builder takes overrides so a test can break exactly one
thing.
"""

import base64
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import cbor2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.core.app_attest import (
    AAGUID_PRODUCTION,
    NONCE_EXTENSION_OID,
    cose_from_public_key,
)

NOW = datetime(2026, 6, 1, tzinfo=UTC)
APP_ID = "TEAM123456.com.example.bookshelf"


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(
    *,
    subject: str,
    issuer: str,
    public_key,
    signing_key,
    ca: bool | None,
    not_before: datetime,
    not_after: datetime,
    extensions: tuple[x509.ExtensionType, ...] = (),
) -> x509.Certificate:
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(_name(issuer))
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
    )
    if ca is not None:  # None: no BasicConstraints extension at all
        builder = builder.add_extension(
            x509.BasicConstraints(ca=ca, path_length=None), critical=True
        )
    for ext in extensions:
        builder = builder.add_extension(ext, critical=False)
    return builder.sign(signing_key, hashes.SHA384() if ca else hashes.SHA256())


@dataclass
class Pki:
    root_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP384R1())
    )
    intermediate_key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP384R1())
    )

    def __post_init__(self) -> None:
        self.root = _cert(
            subject="Test App Attestation Root CA",
            issuer="Test App Attestation Root CA",
            public_key=self.root_key.public_key(),
            signing_key=self.root_key,
            ca=True,
            not_before=NOW - timedelta(days=3650),
            not_after=NOW + timedelta(days=3650),
        )
        self.intermediate = self.make_intermediate()

    @property
    def root_pem(self) -> bytes:
        return self.root.public_bytes(serialization.Encoding.PEM)

    def make_intermediate(
        self, *, ca: bool | None = True, not_after: datetime | None = None
    ) -> x509.Certificate:
        return _cert(
            subject="Test App Attestation CA 1",
            issuer="Test App Attestation Root CA",
            public_key=self.intermediate_key.public_key(),
            signing_key=self.root_key,
            ca=ca,
            not_before=NOW - timedelta(days=365),
            not_after=not_after or NOW + timedelta(days=365),
        )


def nonce_extension(value: bytes) -> x509.UnrecognizedExtension:
    """DER: SEQUENCE { [1] { OCTET STRING value } } (value is 32 bytes)."""
    der = b"\x30\x24\xa1\x22\x04\x20" + value
    return x509.UnrecognizedExtension(NONCE_EXTENSION_OID, der)


def auth_data(
    *,
    app_id: str = APP_ID,
    counter: int = 0,
    aaguid: bytes | None = AAGUID_PRODUCTION,
    credential_id: bytes = b"",
    cose_key: bytes = b"",
) -> bytes:
    """authenticatorData; pass ``aaguid=None`` for an assertion's 37 bytes."""
    head = _sha256(app_id.encode()) + b"\x40" + counter.to_bytes(4, "big")
    if aaguid is None:
        return head
    return (
        head + aaguid + len(credential_id).to_bytes(2, "big") + credential_id + cose_key
    )


@dataclass
class Device:
    """A device key and what it would send to the server."""

    pki: Pki
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    @property
    def key_id_bytes(self) -> bytes:
        point = self.key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        return _sha256(point)

    @property
    def key_id(self) -> str:
        return base64.b64encode(self.key_id_bytes).decode()

    @property
    def public_key_cbor(self) -> bytes:
        return cose_from_public_key(self.key.public_key())

    def attestation(
        self,
        nonce: str,
        *,
        app_id: str = APP_ID,
        counter: int = 0,
        aaguid: bytes = AAGUID_PRODUCTION,
        credential_id: bytes | None = None,
        ext_nonce: bytes | None = None,
        leaf_not_after: datetime | None = None,
        intermediate: x509.Certificate | None = None,
        intermediate_key: ec.EllipticCurvePrivateKey | None = None,
        fmt: str = "apple-appattest",
    ) -> str:
        """A base64 attestation object, valid unless an override breaks it."""
        data = auth_data(
            app_id=app_id,
            counter=counter,
            aaguid=aaguid,
            credential_id=(
                self.key_id_bytes if credential_id is None else credential_id
            ),
            # A COSE key only for P-256: the bad-curve test uses a P-384 key.
            cose_key=(
                self.public_key_cbor
                if isinstance(self.key.curve, ec.SECP256R1)
                else b""
            ),
        )
        if ext_nonce is None:
            ext_nonce = _sha256(data + _sha256(nonce.encode()))
        inter = intermediate or self.pki.intermediate
        leaf = _cert(
            subject="test-leaf",
            issuer=inter.subject.rfc4514_string().removeprefix("CN="),
            public_key=self.key.public_key(),
            signing_key=intermediate_key or self.pki.intermediate_key,
            ca=False,
            not_before=NOW - timedelta(days=1),
            not_after=leaf_not_after or NOW + timedelta(days=30),
            extensions=(nonce_extension(ext_nonce),),
        )
        obj = {
            "fmt": fmt,
            "attStmt": {
                "x5c": [
                    leaf.public_bytes(serialization.Encoding.DER),
                    inter.public_bytes(serialization.Encoding.DER),
                ],
                "receipt": b"test-receipt",
            },
            "authData": data,
        }
        return base64.b64encode(cbor2.dumps(obj)).decode()

    def assertion(
        self,
        nonce: str,
        counter: int,
        *,
        app_id: str = APP_ID,
        signing_key: ec.EllipticCurvePrivateKey | None = None,
    ) -> str:
        """A base64 assertion: signature over SHA256(authData || SHA256(nonce))."""
        data = auth_data(app_id=app_id, counter=counter, aaguid=None)
        signed = _sha256(data + _sha256(nonce.encode()))
        signature = (signing_key or self.key).sign(signed, ec.ECDSA(hashes.SHA256()))
        obj = {"signature": signature, "authenticatorData": data}
        return base64.b64encode(cbor2.dumps(obj)).decode()

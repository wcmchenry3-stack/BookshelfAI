"""Apple App Attest server-side verification (#1051).

Implements the checks in Apple's "Validating apps that connect to your
server" (developer.apple.com/documentation/devicecheck/
validating-apps-that-connect-to-your-server). Each check is numbered as in
that article, so a reviewer can hold the two side by side.

- Attestation (``POST /attestation/register``): the device's one-time proof
  that a key lives in the Secure Enclave of a genuine Apple device running our
  app. On success the leaf public key is stored as a COSE_Key with counter 0.
- Assertion (every iOS ``POST /scan``): a signature by that key over the
  per-request server nonce, with a counter that must strictly increase. The
  caller stores the new counter with a compare-and-set (``_bot_check.py``).

The client (``@expo/app-integrity``) passes the server nonce as the challenge
and the native side hashes its UTF-8 bytes, so ``clientDataHash`` is
``SHA-256(nonce.encode())`` for both steps.

The attestation receipt (``attStmt.receipt``) is decoded but not stored or
used: fraud-metric risk checks against Apple's receipt service are out of
scope. Failures are logged with a reason code only, never key material,
nonces or certificate bytes.
"""

import base64
import binascii
import hashlib
import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import cbor2
from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.attestation import AssertionResult, AttestationResult

logger = logging.getLogger(__name__)

# Apple App Attestation Root CA.
# Source: https://www.apple.com/certificateauthority/Apple_App_Attestation_Root_CA.pem
# SHA-256 fingerprint (DER):
#   1C:B9:82:3B:A2:8B:A6:AD:2D:33:A0:06:94:1D:E2:AE:
#   4F:51:3E:F1:D4:E8:31:B9:F7:E0:FA:7B:62:42:C9:32
# Valid 2020-03-18 to 2045-03-15. tests/unit/test_app_attest.py pins the
# fingerprint, so an edit to this constant fails the suite.
APPLE_APP_ATTEST_ROOT_CA_PEM = b"""-----BEGIN CERTIFICATE-----
MIICITCCAaegAwIBAgIQC/O+DvHN0uD7jG5yH2IXmDAKBggqhkjOPQQDAzBSMSYw
JAYDVQQDDB1BcHBsZSBBcHAgQXR0ZXN0YXRpb24gUm9vdCBDQTETMBEGA1UECgwK
QXBwbGUgSW5jLjETMBEGA1UECAwKQ2FsaWZvcm5pYTAeFw0yMDAzMTgxODMyNTNa
Fw00NTAzMTUwMDAwMDBaMFIxJjAkBgNVBAMMHUFwcGxlIEFwcCBBdHRlc3RhdGlv
biBSb290IENBMRMwEQYDVQQKDApBcHBsZSBJbmMuMRMwEQYDVQQIDApDYWxpZm9y
bmlhMHYwEAYHKoZIzj0CAQYFK4EEACIDYgAERTHhmLW07ATaFQIEVwTtT4dyctdh
NbJhFs/Ii2FdCgAHGbpphY3+d8qjuDngIN3WVhQUBHAoMeQ/cLiP1sOUtgjqK9au
Yen1mMEvRq9Sk3Jm5X8U62H+xTD3FE9TgS41o0IwQDAPBgNVHRMBAf8EBTADAQH/
MB0GA1UdDgQWBBSskRBTM72+aEH/pwyp5frq5eWKoTAOBgNVHQ8BAf8EBAMCAQYw
CgYIKoZIzj0EAwMDaAAwZQIwQgFGnByvsiVbpTKwSga0kP0e8EeDS4+sQmTvb7vn
53O5+FRXgeLhpJ06ysC5PrOyAjEAp5U4xDgEgllF7En3VcE3iexZZtKeYnpqtijV
oyFraWVIyd/dganmrduC1bmTBGwD
-----END CERTIFICATE-----
"""

# Leaf-certificate extension carrying the attestation nonce.
NONCE_EXTENSION_OID = x509.ObjectIdentifier("1.2.840.113635.100.8.2")
# DER prefix of that extension: SEQUENCE(36) { [1](34) { OCTET STRING(32) } }.
_NONCE_EXTENSION_PREFIX = bytes.fromhex("3024a1220420")

AAGUID_PRODUCTION = b"appattest" + b"\x00" * 7
AAGUID_DEVELOPMENT = b"appattestdevelop"

# Upper bounds on the base64 text accepted before decoding. A real attestation
# object (two certificates and a receipt) is about 5.5 KiB of base64; an
# assertion is about 150 bytes.
_MAX_ATTESTATION_B64 = 16 * 1024
_MAX_ASSERTION_B64 = 1024

# authenticatorData layout (WebAuthn): rpIdHash(32) flags(1) signCount(4), then
# for an attestation aaguid(16) credentialIdLength(2) credentialId(n) COSE key.
_RP_ID_HASH_END = 32
_COUNTER_END = 37
_AAGUID_END = 53
_CRED_LEN_END = 55


class AppAttestError(Exception):
    """A failed check. ``reason`` is a short code, safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def decode_b64(value: str, *, max_len: int) -> bytes:
    """Decode standard base64 (what DCAppAttestService returns) or base64url.

    Strict: padding is optional, but any character outside the two alphabets
    is an error (``base64.urlsafe_b64decode`` would silently drop it), and so
    is a string that mixes them (``-``/``_`` with ``+``/``/``).
    """
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise AppAttestError("bad_base64")
    if any(c in value for c in "-_") and any(c in value for c in "+/"):
        raise AppAttestError("bad_base64")
    std = value.replace("-", "+").replace("_", "/").rstrip("=")
    try:
        return base64.b64decode(std + "=" * (-len(std) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AppAttestError("bad_base64") from exc


def canonical_key_id(key_id: str) -> str:
    """The one spelling of an App Attest key id: padded standard base64.

    A key id is SHA-256 of the public key, so it must decode to 32 bytes.
    ``/attestation/register`` stores and ``/scan`` looks up this form, so the
    other spellings ``decode_b64`` accepts (unpadded, base64url) cannot create
    a second row, with its own counter, for the same key.
    """
    raw = decode_b64(key_id, max_len=256)
    if len(raw) != 32:
        raise AppAttestError("bad_key_id")
    return base64.b64encode(raw).decode()


def _cbor_map(raw: bytes) -> dict:
    try:
        decoded = cbor2.loads(raw)
    except Exception as exc:
        raise AppAttestError("bad_cbor") from exc
    if not isinstance(decoded, dict):
        raise AppAttestError("bad_cbor")
    return decoded


def _bytes_field(obj: dict, name: str) -> bytes:
    value = obj.get(name)
    if not isinstance(value, bytes):
        raise AppAttestError("bad_cbor")
    return value


@dataclass(frozen=True)
class _AuthData:
    rp_id_hash: bytes
    counter: int
    aaguid: bytes = b""
    credential_id: bytes = b""


def _parse_auth_data(data: bytes, *, attested: bool) -> _AuthData:
    if len(data) < _COUNTER_END:
        raise AppAttestError("bad_auth_data")
    rp_id_hash = data[:_RP_ID_HASH_END]
    counter = int.from_bytes(data[_RP_ID_HASH_END + 1 : _COUNTER_END], "big")
    if not attested:
        return _AuthData(rp_id_hash=rp_id_hash, counter=counter)
    if len(data) < _CRED_LEN_END:
        raise AppAttestError("bad_auth_data")
    cred_len = int.from_bytes(data[_AAGUID_END:_CRED_LEN_END], "big")
    credential_id = data[_CRED_LEN_END : _CRED_LEN_END + cred_len]
    if len(credential_id) != cred_len:
        raise AppAttestError("bad_auth_data")
    return _AuthData(
        rp_id_hash=rp_id_hash,
        counter=counter,
        aaguid=data[_COUNTER_END:_AAGUID_END],
        credential_id=credential_id,
    )


def _check_validity(cert: x509.Certificate, now: datetime) -> None:
    if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
        raise AppAttestError("cert_expired")


def _verify_chain(
    leaf: x509.Certificate,
    intermediate: x509.Certificate,
    root: x509.Certificate,
    now: datetime,
) -> None:
    """Apple step 1: x5c is [credCert, intermediate], chaining to the root."""
    if not _is_ca(intermediate):
        raise AppAttestError("bad_chain")
    # Defence in depth (not one of Apple's listed checks): the credential
    # certificate is an end-entity certificate, never a CA.
    if _is_ca(leaf):
        raise AppAttestError("bad_chain")
    try:
        # Checks the issuer name and the signature with the issuer's key.
        leaf.verify_directly_issued_by(intermediate)
        intermediate.verify_directly_issued_by(root)
    except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm) as exc:
        raise AppAttestError("bad_chain") from exc
    for cert in (leaf, intermediate, root):
        _check_validity(cert, now)


def _is_ca(cert: x509.Certificate) -> bool:
    try:
        return cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        return False
    except ValueError as exc:  # malformed or duplicate extensions
        raise AppAttestError("bad_chain") from exc


def _nonce_from_extension(leaf: x509.Certificate) -> bytes:
    """The 32-byte octet string in extension 1.2.840.113635.100.8.2."""
    try:
        ext = leaf.extensions.get_extension_for_oid(NONCE_EXTENSION_OID)
    except x509.ExtensionNotFound as exc:
        raise AppAttestError("nonce_missing") from exc
    except ValueError as exc:  # malformed or duplicate extensions
        raise AppAttestError("bad_chain") from exc
    value = getattr(ext.value, "value", b"")
    if len(value) != 38 or not value.startswith(_NONCE_EXTENSION_PREFIX):
        raise AppAttestError("nonce_missing")
    return value[len(_NONCE_EXTENSION_PREFIX) :]


def _p256_key(cert: x509.Certificate) -> ec.EllipticCurvePublicKey:
    try:
        key = cert.public_key()
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise AppAttestError("bad_key") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise AppAttestError("bad_key")
    curve = key.curve
    if not isinstance(curve, ec.SECP256R1):
        raise AppAttestError("bad_key")
    return key


def cose_from_public_key(key: ec.EllipticCurvePublicKey) -> bytes:
    """Encode a P-256 key as a COSE_Key (RFC 9052: EC2, ES256)."""
    numbers = key.public_numbers()
    return cbor2.dumps(
        {
            1: 2,  # kty: EC2
            3: -7,  # alg: ES256
            -1: 1,  # crv: P-256
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        }
    )


def public_key_from_cose(data: bytes) -> ec.EllipticCurvePublicKey:
    cose = _cbor_map(data)
    if cose.get(1) != 2 or cose.get(-1) != 1:
        raise AppAttestError("bad_key")
    x, y = cose.get(-2), cose.get(-3)
    if not (isinstance(x, bytes) and isinstance(y, bytes)):
        raise AppAttestError("bad_key")
    if len(x) != 32 or len(y) != 32:
        raise AppAttestError("bad_key")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), b"\x04" + x + y
        )
    except ValueError as exc:
        raise AppAttestError("bad_key") from exc


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LiveAppleAttestVerifier:
    """Verifies App Attest attestations and assertions.

    ``root_ca_pem`` and ``now`` exist so tests can use their own PKI and
    recorded vectors whose certificates have since expired; production always
    uses the defaults (see ``apple_verifier``).
    """

    def __init__(
        self,
        *,
        allow_development: bool = False,
        root_ca_pem: bytes = APPLE_APP_ATTEST_ROOT_CA_PEM,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._allow_development = allow_development
        self._root = x509.load_pem_x509_certificate(root_ca_pem)
        self._now = now

    async def verify_attestation(
        self, key_id: str, attestation_b64: str, nonce: str, app_id: str
    ) -> AttestationResult | None:
        try:
            return self._attest(key_id, attestation_b64, nonce, app_id)
        except AppAttestError as exc:
            logger.warning("app_attest attestation rejected reason=%s", exc.reason)
            return None

    async def verify_assertion(
        self,
        key_id: str,  # unused: the caller looked the key up by it
        assertion_b64: str,
        public_key_cbor: bytes,
        nonce: str,
        stored_counter: int,
        app_id: str,
    ) -> AssertionResult | None:
        try:
            return self._assert(
                assertion_b64, public_key_cbor, nonce, stored_counter, app_id
            )
        except AppAttestError as exc:
            logger.warning("app_attest assertion rejected reason=%s", exc.reason)
            return None

    def _attest(
        self, key_id: str, attestation_b64: str, nonce: str, app_id: str
    ) -> AttestationResult:
        key_id_bytes = decode_b64(key_id, max_len=256)
        att = _cbor_map(decode_b64(attestation_b64, max_len=_MAX_ATTESTATION_B64))
        if att.get("fmt") != "apple-appattest":
            raise AppAttestError("bad_format")
        att_stmt = att.get("attStmt")
        if not isinstance(att_stmt, dict):
            raise AppAttestError("bad_cbor")
        x5c = att_stmt.get("x5c")
        if not (isinstance(x5c, list) and len(x5c) == 2):
            raise AppAttestError("bad_chain")
        if not all(isinstance(c, bytes) for c in x5c):
            raise AppAttestError("bad_chain")
        auth_data_raw = _bytes_field(att, "authData")
        try:
            leaf = x509.load_der_x509_certificate(x5c[0])
            intermediate = x509.load_der_x509_certificate(x5c[1])
        except ValueError as exc:
            raise AppAttestError("bad_chain") from exc

        # 1. Verify the certificates in x5c, using Apple's App Attestation
        #    root certificate.
        _verify_chain(leaf, intermediate, self._root, self._now())
        # 2. clientDataHash = SHA256(challenge); the challenge is the nonce.
        client_data_hash = _sha256(nonce.encode())
        # 3. nonce = SHA256(authData || clientDataHash).
        expected_nonce = _sha256(auth_data_raw + client_data_hash)
        # 4. It must equal the octet string in the credCert's extension
        #    1.2.840.113635.100.8.2.
        if not hmac.compare_digest(_nonce_from_extension(leaf), expected_nonce):
            raise AppAttestError("nonce_mismatch")
        # 5. SHA256 of the credCert public key (uncompressed point) must equal
        #    the key identifier.
        leaf_key = _p256_key(leaf)
        point = leaf_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        if not hmac.compare_digest(_sha256(point), key_id_bytes):
            raise AppAttestError("key_id_mismatch")
        auth = _parse_auth_data(auth_data_raw, attested=True)
        # 6. RP ID hash must be SHA256 of the App ID (TEAM_ID.BUNDLE_ID).
        if not hmac.compare_digest(auth.rp_id_hash, _sha256(app_id.encode())):
            raise AppAttestError("rp_id_mismatch")
        # 7. The counter must be 0.
        if auth.counter != 0:
            raise AppAttestError("counter_not_zero")
        # 8. aaguid: "appattest" + 7 zero bytes (production) or
        #    "appattestdevelop" (development, only when allowed).
        if auth.aaguid == AAGUID_DEVELOPMENT:
            if not self._allow_development:
                raise AppAttestError("development_aaguid")
        elif auth.aaguid != AAGUID_PRODUCTION:
            raise AppAttestError("bad_aaguid")
        # 9. credentialId must equal the key identifier.
        if not hmac.compare_digest(auth.credential_id, key_id_bytes):
            raise AppAttestError("credential_id_mismatch")
        # Store the public key and a counter of 0 (the receipt is not kept).
        return AttestationResult(
            public_key_cbor=cose_from_public_key(leaf_key), counter=0
        )

    def _assert(
        self,
        assertion_b64: str,
        public_key_cbor: bytes,
        nonce: str,
        stored_counter: int,
        app_id: str,
    ) -> AssertionResult:
        assertion = _cbor_map(decode_b64(assertion_b64, max_len=_MAX_ASSERTION_B64))
        signature = _bytes_field(assertion, "signature")
        auth_data_raw = _bytes_field(assertion, "authenticatorData")
        # 1. clientDataHash = SHA256(clientData); the client data is the nonce.
        client_data_hash = _sha256(nonce.encode())
        # 2. nonce = SHA256(authenticatorData || clientDataHash).
        signed = _sha256(auth_data_raw + client_data_hash)
        # 3. Verify the signature over that nonce with the stored public key
        #    (ECDSA P-256 with SHA-256, as the Secure Enclave signs it).
        key = public_key_from_cose(public_key_cbor)
        try:
            key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
        except InvalidSignature as exc:
            raise AppAttestError("bad_signature") from exc
        auth = _parse_auth_data(auth_data_raw, attested=False)
        # 4. RP ID hash must be SHA256 of the App ID.
        if not hmac.compare_digest(auth.rp_id_hash, _sha256(app_id.encode())):
            raise AppAttestError("rp_id_mismatch")
        # 5. The counter must be greater than the stored one. The caller then
        #    stores it with a compare-and-set, so a concurrent replay fails.
        if auth.counter <= stored_counter:
            raise AppAttestError("counter_replay")
        return AssertionResult(counter=auth.counter)


def apple_verifier() -> LiveAppleAttestVerifier:
    """The production verifier: Apple's root, the real clock."""
    from app.core.config import settings

    return LiveAppleAttestVerifier(
        allow_development=settings.attestation_apple_allow_development
    )

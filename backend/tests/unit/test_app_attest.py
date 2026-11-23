"""Apple App Attest verifier (#1051).

Two kinds of input:

- Device recordings (``tests/fixtures/app_attest_vectors.json``): a
  production and a development attestation and one assertion, made by a real
  iPhone against Apple's servers, so the embedded Apple root, the CBOR layout
  and the signature convention are checked against the real thing. The
  certificates expired in December 2024, so these tests pin the verifier's
  clock to a date inside their validity.
- A test PKI (``tests/_app_attest_pki.py``) for the failure cases: each test
  breaks exactly one of Apple's checks and expects that check's reason code.
"""

import base64
import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import cbor2
import pytest
from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.core import app_attest
from app.core.app_attest import (
    AAGUID_DEVELOPMENT,
    APPLE_APP_ATTEST_ROOT_CA_PEM,
    AppAttestError,
    LiveAppleAttestVerifier,
    cose_from_public_key,
    decode_b64,
    public_key_from_cose,
)
from app.core.attestation import AppleAttestVerifier
from tests._app_attest_pki import APP_ID, NOW, Device, Pki, spellings

VECTORS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "app_attest_vectors.json").read_text()
)
VECTOR_TIME = datetime.fromisoformat(VECTORS["verified_at"])
NONCE = "payload.signature"  # any string: the verifier only hashes it

APPLE_ROOT_SHA256 = "1cb9823ba28ba6ad2d33a006941de2ae4f513ef1d4e831b9f7e0fa7b6242c932"


@pytest.fixture(scope="module")
def pki() -> Pki:
    return Pki()


@pytest.fixture
def device(pki) -> Device:
    return Device(pki)


def _verifier(pki: Pki, **kwargs) -> LiveAppleAttestVerifier:
    return LiveAppleAttestVerifier(root_ca_pem=pki.root_pem, now=lambda: NOW, **kwargs)


def _reason(fn, *args) -> str:
    with pytest.raises(AppAttestError) as exc:
        fn(*args)
    return exc.value.reason


# ---------------------------------------------------------------------------
# Embedded root
# ---------------------------------------------------------------------------


def test_embedded_root_is_apples_app_attestation_root():
    root = x509.load_pem_x509_certificate(APPLE_APP_ATTEST_ROOT_CA_PEM)
    assert root.fingerprint(hashes.SHA256()).hex() == APPLE_ROOT_SHA256
    assert "Apple App Attestation Root CA" in root.subject.rfc4514_string()


def test_production_verifier_uses_apple_root_and_setting(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.settings.attestation_apple_allow_development", False
    )
    verifier = app_attest.apple_verifier()
    assert isinstance(verifier, AppleAttestVerifier)
    assert verifier._root.fingerprint(hashes.SHA256()).hex() == APPLE_ROOT_SHA256
    assert verifier._allow_development is False

    monkeypatch.setattr(
        "app.core.config.settings.attestation_apple_allow_development", True
    )
    assert app_attest.apple_verifier()._allow_development is True


# ---------------------------------------------------------------------------
# Device recordings
# ---------------------------------------------------------------------------


def _vector_verifier(**kwargs) -> LiveAppleAttestVerifier:
    return LiveAppleAttestVerifier(now=lambda: VECTOR_TIME, **kwargs)


async def test_recorded_production_attestation_passes():
    vec = VECTORS["production"]
    result = await _vector_verifier().verify_attestation(
        vec["attested_id"], vec["attestation"], vec["challenge"], VECTORS["app_id"]
    )
    assert result is not None
    assert result.counter == 0
    key = public_key_from_cose(result.public_key_cbor)
    point = key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    assert base64.b64encode(hashlib.sha256(point).digest()).decode() == (
        vec["attested_id"]
    )


async def test_recorded_development_attestation_needs_the_setting(caplog):
    vec = VECTORS["development"]
    args = (vec["attested_id"], vec["attestation"], vec["challenge"])
    with caplog.at_level(logging.WARNING, logger="app.core.app_attest"):
        assert (
            await _vector_verifier().verify_attestation(*args, VECTORS["app_id"])
            is None
        )
    assert "reason=development_aaguid" in caplog.text
    allowed = _vector_verifier(allow_development=True)
    assert await allowed.verify_attestation(*args, VECTORS["app_id"]) is not None


async def test_recorded_attestation_rejected_with_the_real_clock():
    """Same bytes, today's date: Apple's leaf has expired."""
    vec = VECTORS["production"]
    verifier = LiveAppleAttestVerifier()
    assert (
        await verifier.verify_attestation(
            vec["attested_id"], vec["attestation"], vec["challenge"], VECTORS["app_id"]
        )
        is None
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("challenge", "a-different-nonce"),
        ("app_id", "V8H6LQ9448.com.other.app"),
        ("attested_id", VECTORS["development"]["attested_id"]),
    ],
)
async def test_recorded_attestation_rejects_changed_inputs(field, value):
    vec = dict(VECTORS["production"], app_id=VECTORS["app_id"])
    vec[field] = value
    result = await _vector_verifier().verify_attestation(
        vec["attested_id"], vec["attestation"], vec["challenge"], vec["app_id"]
    )
    assert result is None


def _recorded_assertion_key() -> bytes:
    pem = VECTORS["assertion"]["spki_pem"].encode()
    return cose_from_public_key(serialization.load_pem_public_key(pem))


async def test_recorded_assertion_passes_and_returns_its_counter():
    vec = VECTORS["assertion"]
    result = await LiveAppleAttestVerifier().verify_assertion(
        key_id="unused",
        assertion_b64=vec["assertion"],
        public_key_cbor=_recorded_assertion_key(),
        nonce=vec["client_data"],
        stored_counter=0,
        app_id=VECTORS["app_id"],
    )
    assert result is not None
    assert result.counter == 1


@pytest.mark.parametrize(
    ("change", "stored_counter"),
    [({"nonce": "other client data"}, 0), ({}, 1), ({"app_id": "X.y.z"}, 0)],
)
async def test_recorded_assertion_rejects(change, stored_counter):
    vec = VECTORS["assertion"]
    kwargs = {"nonce": vec["client_data"], "app_id": VECTORS["app_id"], **change}
    result = await LiveAppleAttestVerifier().verify_assertion(
        key_id="unused",
        assertion_b64=vec["assertion"],
        public_key_cbor=_recorded_assertion_key(),
        stored_counter=stored_counter,
        **kwargs,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Attestation: each of Apple's checks on its own (test PKI)
# ---------------------------------------------------------------------------


def test_valid_attestation_passes(pki, device):
    result = _verifier(pki)._attest(
        device.key_id, device.attestation(NONCE), NONCE, APP_ID
    )
    assert result.counter == 0
    assert result.public_key_cbor == device.public_key_cbor


def test_base64url_without_padding_is_accepted(pki, device):
    att = device.attestation(NONCE)
    url = att.replace("+", "-").replace("/", "_").rstrip("=")
    kid = device.key_id.replace("+", "-").replace("/", "_").rstrip("=")
    assert _verifier(pki)._attest(kid, url, NONCE, APP_ID).counter == 0


def _attest_reason(pki, device, *, key_id=None, nonce=NONCE, app_id=APP_ID, **kw):
    att = device.attestation(NONCE, **kw)
    return _reason(
        _verifier(pki)._attest,
        key_id or device.key_id,
        att,
        nonce,
        app_id,
    )


def test_chain_to_another_root_rejected(device):
    other = Pki()  # the verifier trusts this root, the device chains to its own
    reason = _reason(
        _verifier(other)._attest,
        device.key_id,
        device.attestation(NONCE),
        NONCE,
        APP_ID,
    )
    assert reason == "bad_chain"


def test_leaf_not_signed_by_intermediate_rejected(pki, device):
    rogue = ec.generate_private_key(ec.SECP384R1())
    assert _attest_reason(pki, device, intermediate_key=rogue) == "bad_chain"


@pytest.mark.parametrize("ca", [False, None])
def test_intermediate_that_is_not_a_ca_rejected(pki, device, ca):
    not_ca = pki.make_intermediate(ca=ca)
    assert _attest_reason(pki, device, intermediate=not_ca) == "bad_chain"


def test_expired_leaf_rejected(pki, device):
    expired = NOW - timedelta(seconds=1)
    assert _attest_reason(pki, device, leaf_not_after=expired) == "cert_expired"


def test_expired_intermediate_rejected(pki, device):
    old = pki.make_intermediate(not_after=NOW - timedelta(days=1))
    assert _attest_reason(pki, device, intermediate=old) == "cert_expired"


def test_nonce_mismatch_rejected(pki, device):
    assert _attest_reason(pki, device, nonce="another.nonce") == "nonce_mismatch"


def test_nonce_extension_for_other_auth_data_rejected(pki, device):
    assert _attest_reason(pki, device, ext_nonce=b"\x00" * 32) == "nonce_mismatch"


def test_key_id_mismatch_rejected(pki, device):
    other = Device(pki).key_id
    assert _attest_reason(pki, device, key_id=other) == "key_id_mismatch"


def test_rp_id_mismatch_rejected(pki, device):
    assert _attest_reason(pki, device, app_id="TEAM.other.app") == "rp_id_mismatch"


def test_counter_not_zero_rejected(pki, device):
    assert _attest_reason(pki, device, counter=1) == "counter_not_zero"


def test_development_aaguid_rejected_by_default(pki, device):
    att = device.attestation(NONCE, aaguid=AAGUID_DEVELOPMENT)
    reason = _reason(_verifier(pki)._attest, device.key_id, att, NONCE, APP_ID)
    assert reason == "development_aaguid"
    allowed = _verifier(pki, allow_development=True)
    assert allowed._attest(device.key_id, att, NONCE, APP_ID).counter == 0


def test_unknown_aaguid_rejected(pki, device):
    assert _attest_reason(pki, device, aaguid=b"x" * 16) == "bad_aaguid"


def test_credential_id_mismatch_rejected(pki, device):
    other = Device(pki).key_id_bytes
    assert _attest_reason(pki, device, credential_id=other) == "credential_id_mismatch"


def test_wrong_format_rejected(pki, device):
    assert _attest_reason(pki, device, fmt="packed") == "bad_format"


def _cbor_b64(obj) -> str:
    return base64.b64encode(cbor2.dumps(obj)).decode()


@pytest.mark.parametrize(
    ("attestation", "reason"),
    [
        ("not base64 !", "bad_base64"),
        ("A" * 20000, "bad_base64"),
        (base64.b64encode(b"\xff\xff").decode(), "bad_cbor"),
        (_cbor_b64([1, 2]), "bad_cbor"),
        (_cbor_b64({"fmt": "apple-appattest", "attStmt": []}), "bad_cbor"),
        (
            _cbor_b64({"fmt": "apple-appattest", "attStmt": {"x5c": [b"a"]}}),
            "bad_chain",
        ),
        (
            _cbor_b64({"fmt": "apple-appattest", "attStmt": {"x5c": ["a", "b"]}}),
            "bad_chain",
        ),
        (
            _cbor_b64(
                {
                    "fmt": "apple-appattest",
                    "attStmt": {"x5c": [b"a", b"b"]},
                    "authData": "text",
                }
            ),
            "bad_cbor",
        ),
        (
            _cbor_b64(
                {
                    "fmt": "apple-appattest",
                    "attStmt": {"x5c": [b"a", b"b"]},
                    "authData": b"",
                }
            ),
            "bad_chain",
        ),
    ],
)
def test_malformed_attestation_rejected(pki, device, attestation, reason):
    verifier = _verifier(pki)
    assert _reason(verifier._attest, device.key_id, attestation, NONCE, APP_ID) == (
        reason
    )


def test_leaf_without_nonce_extension_rejected(pki, device, monkeypatch):
    monkeypatch.setattr(
        "tests._app_attest_pki.nonce_extension",
        lambda _v: x509.UnrecognizedExtension(
            x509.ObjectIdentifier("1.2.3.4"), b"\x04\x00"
        ),
    )
    assert _attest_reason(pki, device) == "nonce_missing"


def test_nonce_extension_with_wrong_shape_rejected(pki, device, monkeypatch):
    monkeypatch.setattr(
        "tests._app_attest_pki.nonce_extension",
        lambda v: x509.UnrecognizedExtension(
            app_attest.NONCE_EXTENSION_OID, b"\x04\x20" + v
        ),
    )
    assert _attest_reason(pki, device) == "nonce_missing"


def test_p384_leaf_key_rejected(pki):
    device = Device(pki, key=ec.generate_private_key(ec.SECP384R1()))
    assert _attest_reason(pki, device) == "bad_key"


@pytest.mark.parametrize(
    ("data", "attested"),
    [
        (b"\x00" * 36, False),
        (b"\x00" * 40, True),
        (b"\x00" * 53 + b"\x00\x20" + b"\x01" * 5, True),
    ],
)
def test_short_auth_data_rejected(data, attested):
    with pytest.raises(AppAttestError) as exc:
        app_attest._parse_auth_data(data, attested=attested)
    assert exc.value.reason == "bad_auth_data"


async def test_verify_attestation_logs_reason_only(pki, device, caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.app_attest"):
        result = await _verifier(pki).verify_attestation(
            device.key_id, device.attestation(NONCE), "other.nonce", APP_ID
        )
    assert result is None
    assert caplog.messages == ["app_attest attestation rejected reason=nonce_mismatch"]


# ---------------------------------------------------------------------------
# Assertion
# ---------------------------------------------------------------------------


async def _assert(pki, device, assertion, *, stored=0, nonce=NONCE, key=None):
    return await _verifier(pki).verify_assertion(
        key_id=device.key_id,
        assertion_b64=assertion,
        public_key_cbor=key or device.public_key_cbor,
        nonce=nonce,
        stored_counter=stored,
        app_id=APP_ID,
    )


async def test_valid_assertion_returns_new_counter(pki, device):
    result = await _assert(pki, device, device.assertion(NONCE, 5), stored=4)
    assert result is not None
    assert result.counter == 5


def _assert_reason(pki, device, assertion, *, stored=0, nonce=NONCE, key=None):
    return _reason(
        _verifier(pki)._assert,
        assertion,
        key or device.public_key_cbor,
        nonce,
        stored,
        APP_ID,
    )


def test_assertion_signed_by_another_key_rejected(pki, device):
    rogue = ec.generate_private_key(ec.SECP256R1())
    assertion = device.assertion(NONCE, 1, signing_key=rogue)
    assert _assert_reason(pki, device, assertion) == "bad_signature"


def test_assertion_for_another_nonce_rejected(pki, device):
    assertion = device.assertion(NONCE, 1)
    assert _assert_reason(pki, device, assertion, nonce="x.y") == "bad_signature"


@pytest.mark.parametrize("stored", [5, 6])
def test_assertion_counter_replay_rejected(pki, device, stored):
    assertion = device.assertion(NONCE, 5)
    assert _assert_reason(pki, device, assertion, stored=stored) == "counter_replay"


def test_assertion_rp_id_mismatch_rejected(pki, device):
    assertion = device.assertion(NONCE, 1, app_id="TEAM.other.app")
    assert _assert_reason(pki, device, assertion) == "rp_id_mismatch"


@pytest.mark.parametrize(
    ("assertion", "reason"),
    [
        ("", "bad_base64"),
        ("A" * 2000, "bad_base64"),
        (_cbor_b64({"signature": b"x"}), "bad_cbor"),
        (_cbor_b64({"signature": "x", "authenticatorData": b""}), "bad_cbor"),
    ],
)
def test_malformed_assertion_rejected(pki, device, assertion, reason):
    assert _assert_reason(pki, device, assertion) == reason


@pytest.mark.parametrize(
    "cose",
    [
        cbor2.dumps({1: 3}),
        cbor2.dumps({1: 2, -1: 1, -2: "x", -3: b"y"}),
        cbor2.dumps({1: 2, -1: 1, -2: b"x", -3: b"y"}),
        cbor2.dumps({1: 2, -1: 1, -2: b"\x00" * 32, -3: b"\x00" * 32}),
    ],
)
def test_bad_stored_key_rejected(cose):
    with pytest.raises(AppAttestError) as exc:
        public_key_from_cose(cose)
    assert exc.value.reason == "bad_key"


async def test_verify_assertion_logs_reason_only(pki, device, caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.app_attest"):
        result = await _assert(pki, device, device.assertion(NONCE, 3), stored=3)
    assert result is None
    assert caplog.messages == ["app_attest assertion rejected reason=counter_replay"]


def test_decode_b64_rejects_non_string():
    with pytest.raises(AppAttestError):
        decode_b64(None, max_len=10)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Review follow-ups (PR #1074): key_id spelling, chain shape, error wrapping
# ---------------------------------------------------------------------------


def test_every_key_id_spelling_has_one_canonical_form(device):
    forms = {app_attest.canonical_key_id(s) for s in spellings(device.key_id)}
    assert forms == {device.key_id}


@pytest.mark.parametrize(
    "key_id",
    [
        base64.b64encode(b"\x01" * 31).decode(),
        base64.b64encode(b"\x01" * 33).decode(),
        "not base64 !",
    ],
)
def test_key_id_that_is_not_32_bytes_rejected(key_id):
    with pytest.raises(AppAttestError):
        app_attest.canonical_key_id(key_id)


@pytest.mark.parametrize("value", ["ab+c-def", "ab/c_def", "a-b+"])
def test_mixed_base64_alphabets_rejected(value):
    with pytest.raises(AppAttestError) as exc:
        decode_b64(value, max_len=100)
    assert exc.value.reason == "bad_base64"


def test_x5c_with_three_certificates_rejected(pki, device):
    extra = pki.root.public_bytes(serialization.Encoding.DER)
    assert _attest_reason(pki, device, extra_x5c=(extra,)) == "bad_chain"


def test_expired_root_rejected():
    expired = Pki(root_not_after=NOW - timedelta(days=1))
    device = Device(expired)
    reason = _reason(
        _verifier(expired)._attest,
        device.key_id,
        device.attestation(NONCE),
        NONCE,
        APP_ID,
    )
    assert reason == "cert_expired"


def test_leaf_that_is_a_ca_rejected(pki, device):
    assert _attest_reason(pki, device, leaf_ca=True) == "bad_chain"


class _BrokenCert:
    """A certificate whose extensions and key cannot be parsed."""

    @property
    def extensions(self):
        raise ValueError("duplicate extension")

    def public_key(self):
        raise UnsupportedAlgorithm("unknown key type")


@pytest.mark.parametrize(
    ("check", "reason"),
    [
        (app_attest._is_ca, "bad_chain"),
        (app_attest._nonce_from_extension, "bad_chain"),
        (app_attest._p256_key, "bad_key"),
    ],
)
def test_unparseable_certificate_parts_are_clean_rejections(check, reason):
    assert _reason(check, _BrokenCert()) == reason


def test_non_ec_leaf_key_rejected():
    from cryptography.hazmat.primitives.asymmetric import ed25519

    class _Ed25519Cert:
        def public_key(self):
            return ed25519.Ed25519PrivateKey.generate().public_key()

    assert _reason(app_attest._p256_key, _Ed25519Cert()) == "bad_key"

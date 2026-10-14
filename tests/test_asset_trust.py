"""Trust is explicit origin policy, never permission to execute an asset."""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib
import json
import re
import subprocess

import pytest


KEY_TYPE = "progenitor-rsa-pkcs1-sha256-v1"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _integer(value):
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_test_identity(directory):
    """Make an isolated test RSA identity and PEM files under the caller's temp path."""
    directory.mkdir(parents=True, exist_ok=True)
    private = directory / "private.pem"
    public = directory / "public.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(private), "2048"],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "rsa", "-in", str(private), "-pubout", "-out", str(public)],
                   check=True, capture_output=True)
    description = subprocess.run(["openssl", "rsa", "-in", str(private), "-text", "-noout"],
                                 check=True, capture_output=True, text=True).stdout

    def component(name):
        section = re.search(rf"{name}:\n((?:\s+[0-9a-f:]+\n)+)", description).group(1)
        return int(re.sub(r"\s|:", "", section), 16)

    public_key = {"key_type": KEY_TYPE, "n": _integer(component("modulus")), "e": 65537}
    fingerprint = hashlib.sha256(_canonical(public_key)).hexdigest()
    identity = {"schema_version": "akashic.node-identity/v1", "node_id": fingerprint,
                "key_type": KEY_TYPE, "public_key": public_key, "public_key_id": fingerprint,
                "private_key": {"d": _integer(component("privateExponent"))}}
    return identity, private, public


@pytest.fixture(scope="session")
def signing_key(tmp_path_factory):
    return generate_test_identity(tmp_path_factory.mktemp("trust-identity"))


@pytest.fixture(scope="session")
def trusted_identity(signing_key):
    return signing_key[0]


@pytest.fixture
def api():
    def call(module, function, *args, **kwargs):
        try:
            method = getattr(importlib.import_module(module), function)
        except (ImportError, AttributeError):
            pytest.fail("source-verification API missing")
        return method(*args, **kwargs)

    return (lambda *a, **kw: call("rpe_harness.assets.identity", "sign_envelope", *a, **kw),
            lambda *a, **kw: call("rpe_harness.assets.trust", "verify_envelope", *a, **kw))


@pytest.fixture
def policy(signing_key):
    identity = signing_key[0]
    return {"schema_version": "rpe.trust-policy/v1", "trusted_keys": [
        {"key_id": identity["public_key_id"], "owner": "local creator",
         "roles": ["creator"], "scopes": ["capability-package"],
         "public_key": copy.deepcopy(identity["public_key"]), "revoked": False}]}


def _verify(api, envelope, policy, *, scope="capability-package", role="creator"):
    return api[1](envelope, policy, scope=scope, role=role)


def test_explicit_creator_origin_and_wrapper(api, signing_key, policy):
    document = {"schema_version": "rpe.capability-package/v1", "name": "demo/转换"}
    envelope = api[0](document, signing_key[0], scope="capability-package")
    assert envelope["document"] == document
    assert set(envelope) == {"schema_version", "scope", "document", "signature"}
    assert envelope["schema_version"] == "rpe.signed-document/v1"
    result = _verify(api, envelope, policy)
    assert result == {"passed": True, "owner": "local creator",
                      "key_id": signing_key[0]["public_key_id"], "reason": "trusted_origin"}
    assert "active" not in result and "executable" not in result


def test_signatures_interoperate_with_openssl(api, signing_key, policy, tmp_path):
    identity, private, public = signing_key
    envelope = api[0]({"name": "demo/csv", "value": "中文"}, identity,
                      scope="capability-package")
    payload = {key: value for key, value in envelope.items() if key != "signature"}
    message = tmp_path / "message.json"
    signature = tmp_path / "signature.bin"
    message.write_bytes(_canonical(payload))
    value = envelope["signature"]["value"]
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    signature.write_bytes(raw.rjust(256, b"\0"))
    checked = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public),
                              "-signature", str(signature), str(message)], capture_output=True)
    assert checked.returncode == 0, checked.stderr
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private),
                    "-out", str(signature), str(message)], check=True, capture_output=True)
    envelope["signature"]["value"] = _integer(int.from_bytes(signature.read_bytes(), "big"))
    assert _verify(api, envelope, policy)["passed"]


def test_explicit_identity_and_policy_files(api, signing_key, policy, tmp_path):
    identity_file = tmp_path / "identity.json"
    policy_file = tmp_path / "keyring.json"
    identity_file.write_text(json.dumps(signing_key[0]))
    policy_file.write_text(json.dumps(policy))
    envelope = api[0]({"name": "demo/csv"}, identity_file, scope="capability-package")
    assert _verify(api, envelope, policy_file)["passed"]


@pytest.mark.parametrize("mutation", ["unknown", "self_attested", "revoked", "role", "scope",
                                     "key_id", "owner", "missing_revoked", "duplicate"])
def test_policy_rejects_unauthorized_sources(api, signing_key, policy, mutation):
    envelope = api[0]({"name": "demo/csv"}, signing_key[0], scope="capability-package")
    record = policy["trusted_keys"][0]
    if mutation == "unknown":
        policy["trusted_keys"] = []
    elif mutation == "self_attested":
        envelope["public_key"] = signing_key[0]["public_key"]
        policy["trusted_keys"] = []
    elif mutation == "revoked":
        record["revoked"] = True
    elif mutation == "role":
        record["roles"] = ["registry"]
    elif mutation == "scope":
        record["scopes"] = ["registry-index"]
    elif mutation == "key_id":
        record["key_id"] = "0" * 64
    elif mutation == "owner":
        record["owner"] = ""
    elif mutation == "missing_revoked":
        del record["revoked"]
    elif mutation == "duplicate":
        policy["trusted_keys"].append(copy.deepcopy(record))
    result = _verify(api, envelope, policy)
    assert result["passed"] is False and result["reason"]
    assert result["owner"] is None


@pytest.mark.parametrize("scope,role", [("registry-index", "creator"),
                                      ("capability-package", "registry"),
                                      ("unknown", "creator"),
                                      ("capability-package", "unknown")])
def test_expected_use_cannot_be_crossed(api, signing_key, policy, scope, role):
    envelope = api[0]({"name": "demo/csv"}, signing_key[0], scope="capability-package")
    policy["trusted_keys"][0]["roles"] = ["creator", "registry"]
    policy["trusted_keys"][0]["scopes"] = ["capability-package", "registry-index"]
    assert not _verify(api, envelope, policy, scope=scope, role=role)["passed"]


def test_registry_requires_its_own_role_and_scope(api, signing_key, policy):
    envelope = api[0]({"entries": []}, signing_key[0], scope="registry-index")
    record = policy["trusted_keys"][0]
    record["roles"] = ["registry"]
    record["scopes"] = ["registry-index"]
    assert _verify(api, envelope, policy, scope="registry-index", role="registry")["passed"]
    envelope["scope"] = "capability-package"
    record["roles"] = ["creator"]
    record["scopes"] = ["capability-package"]
    assert not _verify(api, envelope, policy)["passed"]


@pytest.mark.parametrize("mutation", ["document", "wrapper_schema", "document_type", "extra",
                                     "algorithm", "signature_schema", "key_type", "key_id",
                                     "signature_extra", "signature_value", "padded", "nonalphabet",
                                     "leading_zero", "at_modulus", "oversize", "missing"])
def test_tampered_or_malformed_envelope_fails_closed(api, signing_key, policy, mutation):
    envelope = api[0]({"name": "demo/csv"}, signing_key[0], scope="capability-package")
    signature = envelope["signature"]
    if mutation == "document":
        envelope["document"]["name"] = "attacker/csv"
    elif mutation == "wrapper_schema":
        envelope["schema_version"] = "unknown/v1"
    elif mutation == "document_type":
        envelope["document"] = []
    elif mutation == "extra":
        envelope["trust_state"] = "registered"
    elif mutation in {"algorithm", "signature_schema", "key_type", "key_id"}:
        field = {"signature_schema": "schema_version", "key_id": "public_key_id"}.get(mutation,
                                                                                         mutation)
        signature[field] = "unknown"
    elif mutation == "signature_extra":
        signature["public_key"] = signing_key[0]["public_key"]
    elif mutation == "signature_value":
        signature["value"] = "AQ"
    elif mutation == "padded":
        signature["value"] += "="
    elif mutation == "nonalphabet":
        signature["value"] += "!"
    elif mutation == "leading_zero":
        value = signature["value"]
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        signature["value"] = base64.urlsafe_b64encode(b"\0" + raw).rstrip(b"=").decode()
    elif mutation == "at_modulus":
        signature["value"] = signing_key[0]["public_key"]["n"]
    elif mutation == "oversize":
        signature["value"] = "A" * 2000000
    elif mutation == "missing":
        del envelope["signature"]
    assert not _verify(api, envelope, policy)["passed"]


@pytest.mark.parametrize("mutation", ["weak", "huge", "even", "e_one", "e_even", "e_huge",
                                     "e_string", "e_bool", "noncanonical_n", "unknown_type"])
def test_malformed_public_keys_fail_before_rsa_work(api, signing_key, policy, mutation):
    envelope = api[0]({"name": "demo/csv"}, signing_key[0], scope="capability-package")
    key = policy["trusted_keys"][0]["public_key"]
    if mutation == "weak":
        key["n"] = _integer((1 << 1023) + 1)
    elif mutation == "huge":
        key["n"] = "A" * 2000000
    elif mutation == "even":
        key["n"] = _integer(1 << 2047)
    elif mutation.startswith("e_"):
        key["e"] = {"e_one": 1, "e_even": 4, "e_huge": 1 << 100000,
                    "e_string": "65537", "e_bool": True}[mutation]
    elif mutation == "noncanonical_n":
        key["n"] += "="
    elif mutation == "unknown_type":
        key["key_type"] = "textbook-rsa"
    assert not _verify(api, envelope, policy)["passed"]


@pytest.mark.parametrize("bad", [None, [], {}, {"schema_version": "wrong", "trusted_keys": []}])
def test_invalid_policy_is_an_explicit_failure(api, signing_key, bad):
    envelope = api[0]({"name": "demo/csv"}, signing_key[0], scope="capability-package")
    assert not _verify(api, envelope, bad)["passed"]


def test_old_scope_less_signature_is_not_new_trust(api, signing_key, policy, tmp_path):
    payload = {"schema_version": "akashic.registry-index/v1", "entries": []}
    message = tmp_path / "legacy.json"
    signature_file = tmp_path / "legacy.sig"
    message.write_bytes(_canonical(payload))
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(signing_key[1]),
                    "-out", str(signature_file), str(message)], check=True, capture_output=True)
    payload["signature"] = {"schema_version": "akashic.signature/v1", "key_type": KEY_TYPE,
                            "algorithm": "rsa-pkcs1-sha256",
                            "public_key_id": signing_key[0]["public_key_id"],
                            "value": _integer(int.from_bytes(signature_file.read_bytes(), "big"))}
    record = policy["trusted_keys"][0]
    record.update(roles=["registry"], scopes=["registry-index"])
    assert not _verify(api, payload, policy, scope="registry-index", role="registry")["passed"]


def test_signer_rejects_wrong_fingerprint_private_key_or_document(api, signing_key):
    for mutation in ("fingerprint", "private", "scope", "document"):
        identity = copy.deepcopy(signing_key[0])
        scope = "capability-package"
        document = {"name": "demo/csv"}
        if mutation == "fingerprint":
            identity["public_key_id"] = "0" * 64
        elif mutation == "private":
            identity["private_key"]["d"] = "AQ"
        elif mutation == "scope":
            scope = "invalid"
        elif mutation == "document":
            document = {"value": float("nan")}
        with pytest.raises(ValueError):
            api[0](document, identity, scope=scope)


def test_oversize_or_duplicate_json_policy_file_rejected(api, signing_key, tmp_path):
    envelope = api[0]({"name": "demo/csv"}, signing_key[0], scope="capability-package")
    path = tmp_path / "policy.json"
    path.write_text('{"schema_version":"rpe.trust-policy/v1","trusted_keys":[],"trusted_keys":[]}')
    assert not _verify(api, envelope, path)["passed"]
    path.write_text(" " * 1100000)
    assert not _verify(api, envelope, path)["passed"]


def test_signed_document_is_snapshot_and_must_be_json(api, signing_key, policy):
    document = {"name": "demo/csv", "parents": []}
    envelope = api[0](document, signing_key[0], scope="capability-package")
    document["parents"].append("modified")
    assert _verify(api, envelope, policy)["passed"]
    for bad in ([], {"x": b"binary"}, {1: "non-string-key"}, {"x": "x" * 1100000}):
        with pytest.raises(ValueError):
            api[0](bad, signing_key[0], scope="capability-package")


def test_signer_does_not_emit_oversize_envelope(api, signing_key):
    # Payload fits, but RSA signature fields push the finished envelope past 1 MiB.
    with pytest.raises(ValueError):
        api[0]({"value": "x" * (1024 * 1024 - 200)}, signing_key[0],
               scope="capability-package")


def test_file_policy_revocation_is_rechecked(api, signing_key, policy, tmp_path):
    envelope = api[0]({"name": "demo/csv"}, signing_key[0], scope="capability-package")
    policy_file = tmp_path / "trust.json"
    policy_file.write_text(json.dumps(policy))
    assert _verify(api, envelope, policy_file)["passed"]
    policy["trusted_keys"][0]["revoked"] = True
    policy_file.write_text(json.dumps(policy))
    result = _verify(api, envelope, policy_file)
    assert result["passed"] is False and result["reason"] == "revoked_key"


@pytest.mark.parametrize("document", [
    {"values": [(1 << 8191) - 1] * 1000},
    {"value": "\x01" * 200000},
])
def test_canonical_encoder_stops_consuming_at_byte_budget(monkeypatch, document):
    from rpe_harness.assets.identity import canonical_json

    # Trace the real encoder: collecting every chunk, or using its one-shot
    # full-output path, exceeds this bound before the final size rejection.
    iterencode = json.JSONEncoder.iterencode
    emitted_bytes = 0

    def observed_iterencode(self, value, *args, **kwargs):
        nonlocal emitted_bytes
        for chunk in iterencode(self, value, *args, **kwargs):
            emitted_bytes += len(chunk.encode("utf-8"))
            yield chunk

    monkeypatch.setattr(json.JSONEncoder, "iterencode", observed_iterencode)
    with pytest.raises(ValueError):
        canonical_json(document)
    # The 8192-bit integer limit bounds the last decimal chunk below 4096 bytes.
    assert emitted_bytes <= 1024 * 1024 + 4096


def test_incremental_canonical_json_preserves_exact_signing_bytes():
    from rpe_harness.assets.identity import canonical_json

    assert canonical_json({"z": "中文\n\x01", "a": [True, None, 123, -1.25]}) == (
        b'{"a":[true,null,123,-1.25],"z":"' + "中文".encode() + b'\\n\\u0001"}'
    )

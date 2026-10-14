"""Explicit local origin policy. A passing signature never authorizes execution."""
from __future__ import annotations

import os
import re

from .identity import KEY_TYPE, _verify_signature, canonical_json, load_local_json, validate_public_key


_USES = {"capability-package": "creator", "registry-index": "registry"}
_KEY_ID = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def _policy_records(policy: dict) -> list[tuple[dict, int, int]]:
    if (set(policy) != {"schema_version", "trusted_keys"}
            or policy.get("schema_version") != "rpe.trust-policy/v1"):
        raise ValueError("invalid_trust_policy")
    records = policy["trusted_keys"]
    if type(records) is not list or len(records) > 256:
        raise ValueError("invalid_trust_policy")
    validated = []
    seen = set()
    for record in records:
        if (type(record) is not dict or set(record) !=
                {"key_id", "owner", "roles", "scopes", "public_key", "revoked"}):
            raise ValueError("invalid_trust_record")
        key_id = record["key_id"]
        if (type(key_id) is not str or not _KEY_ID.fullmatch(key_id)
                or key_id in seen):
            raise ValueError("invalid_or_duplicate_key_id")
        seen.add(key_id)
        owner = record["owner"]
        if type(owner) is not str or not owner.strip() or len(owner) > 256:
            raise ValueError("invalid_owner")
        if type(record["revoked"]) is not bool:
            raise ValueError("invalid_revocation_state")
        for field, allowed in (("roles", set(_USES.values())), ("scopes", set(_USES))):
            values = record[field]
            if (type(values) is not list or not values or len(values) > len(allowed)
                    or any(type(value) is not str or value not in allowed for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError("invalid_trust_use")
        modulus, exponent, fingerprint = validate_public_key(record["public_key"])
        if key_id != fingerprint:
            raise ValueError("key_fingerprint_mismatch")
        validated.append((record, modulus, exponent))
    return validated


def verify_envelope(envelope: dict, keyring: dict | str | os.PathLike, *, scope: str,
                    role: str) -> dict:
    """Check wrapper, algorithm, key possession and both locally granted uses.

    ``keyring`` has no default and is reread on every call. In-envelope public
    keys and old scope-less signatures cannot become trust roots.
    """
    key_id = None
    try:
        if (type(scope) is not str or type(role) is not str
                or _USES.get(scope) != role):
            raise ValueError("unsupported_scope_or_role")
        canonical_json(envelope)
        if (set(envelope) != {"schema_version", "scope", "document", "signature"}
                or envelope["schema_version"] != "rpe.signed-document/v1"
                or envelope["scope"] != scope or type(envelope["document"]) is not dict):
            raise ValueError("invalid_signed_envelope")
        signature = envelope["signature"]
        if (type(signature) is not dict or set(signature) !=
                {"schema_version", "key_type", "public_key_id", "algorithm", "value"}
                or signature["schema_version"] != "akashic.signature/v1"
                or signature["key_type"] != KEY_TYPE
                or signature["algorithm"] != "rsa-pkcs1-sha256"):
            raise ValueError("unsupported_signature")
        claimed_id = signature["public_key_id"]
        if type(claimed_id) is not str or not _KEY_ID.fullmatch(claimed_id):
            raise ValueError("invalid_signature_key_id")
        key_id = claimed_id
        records = _policy_records(load_local_json(keyring))
        selected = next((entry for entry in records if entry[0]["key_id"] == key_id), None)
        if selected is None:
            raise ValueError("unknown_key")
        record, modulus, exponent = selected
        if record["revoked"]:
            raise ValueError("revoked_key")
        if role not in record["roles"] or scope not in record["scopes"]:
            raise ValueError("unauthorized_scope_or_role")
        payload = {key: value for key, value in envelope.items() if key != "signature"}
        if not _verify_signature(payload, signature, modulus, exponent):
            raise ValueError("invalid_signature")
        return {"passed": True, "owner": record["owner"], "key_id": key_id,
                "reason": "trusted_origin"}
    except (ValueError, TypeError, KeyError, RecursionError) as error:
        return {"passed": False, "owner": None, "key_id": key_id,
                "reason": str(error) if isinstance(error, ValueError) else "invalid_signed_input"}

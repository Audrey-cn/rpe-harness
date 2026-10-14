"""Bounded local RSA signing; no identity creation, runtime imports, or global state.

PKCS#1 v1.5 SHA256 encoding, canonical JSON and key fingerprints adapted from
Audrey-cn/progenitor-protocol, hatchery/stargate_identity.py at
84bc86e121aac5c71a53abc23189cc07e562b256. See THIRD_PARTY_NOTICES.md.

MIT License
Copyright (c) 2026 Audrey (Audrey-cn)
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import stat


KEY_TYPE = "progenitor-rsa-pkcs1-sha256-v1"
MAX_JSON_BYTES = 1024 * 1024
MAX_KEY_BITS = 8192
SCOPES = frozenset({"capability-package", "registry-index"})
_SHA256_DIGESTINFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
_B64URL = re.compile(r"[A-Za-z0-9_-]+\Z", re.ASCII)


def canonical_json(document: dict) -> bytes:
    """Original canonical encoding, restricted to bounded finite JSON objects."""
    if type(document) is not dict:
        raise ValueError("invalid_document")
    stack = [(document, 0)]
    nodes = 0
    text_bytes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > 100000 or depth > 64:
            raise ValueError("document_too_complex")
        if type(value) is dict:
            if len(value) > 100000:
                raise ValueError("document_too_complex")
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("invalid_json_key")
                stack.append((key, depth + 1))
                stack.append((item, depth + 1))
        elif type(value) is list:
            if len(value) > 100000:
                raise ValueError("document_too_complex")
            stack.extend((item, depth + 1) for item in value)
        elif type(value) is str:
            if len(value) > MAX_JSON_BYTES:
                raise ValueError("document_too_large")
            try:
                text_bytes += len(value.encode("utf-8")) + 2
            except UnicodeError as error:
                raise ValueError("invalid_json_string") from error
            if text_bytes > MAX_JSON_BYTES:
                raise ValueError("document_too_large")
            # Account for JSON escapes before the encoder constructs a string
            # chunk. Each scalar chunk then fits the same total byte budget.
            text_bytes += sum(1 if char in '"\\\b\f\n\r\t' else 5 if ord(char) < 32 else 0
                              for char in value)
            if text_bytes > MAX_JSON_BYTES:
                raise ValueError("document_too_large")
        elif type(value) is int:
            if value.bit_length() > MAX_KEY_BITS:
                raise ValueError("json_integer_too_large")
        elif type(value) is float:
            if not math.isfinite(value):
                raise ValueError("invalid_json_number")
        elif value is not None and type(value) is not bool:
            raise ValueError("invalid_json_value")
    encoded = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                               allow_nan=False)
    try:
        for chunk in encoder.iterencode(document):
            encoded_chunk = chunk.encode("utf-8")
            if len(encoded) + len(encoded_chunk) > MAX_JSON_BYTES:
                raise ValueError("document_too_large")
            encoded.extend(encoded_chunk)
    except (TypeError, RecursionError, UnicodeError) as error:
        raise ValueError("invalid_json") from error
    return bytes(encoded)


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def load_local_json(source: dict | str | os.PathLike) -> dict:
    """Read explicit local JSON afresh, bounded and without following file symlinks."""
    if type(source) is dict:
        canonical_json(source)
        return source
    if not isinstance(source, (str, os.PathLike)):
        raise ValueError("invalid_json_source")
    descriptor = None
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_JSON_BYTES:
            raise ValueError("invalid_json_file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(MAX_JSON_BYTES + 1)
        if len(content) > MAX_JSON_BYTES:
            raise ValueError("document_too_large")
        result = json.loads(content, object_pairs_hook=_unique_object)
        canonical_json(result)
        return result
    except (OSError, UnicodeError, TypeError, RecursionError) as error:
        raise ValueError("invalid_json_file") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def int_to_b64u(value: int) -> str:
    size = max(1, (value.bit_length() + 7) // 8)
    return base64.urlsafe_b64encode(value.to_bytes(size, "big")).rstrip(b"=").decode("ascii")


def _bounded_integer(value: str, *, max_bytes: int) -> int:
    if (type(value) is not str or not value or len(value) > (max_bytes * 8 + 5) // 6
            or not _B64URL.fullmatch(value)):
        raise ValueError("invalid_base64_integer")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid_base64_integer") from error
    if not raw or len(raw) > max_bytes or (len(raw) > 1 and raw[0] == 0):
        raise ValueError("noncanonical_base64_integer")
    integer = int.from_bytes(raw, "big")
    if int_to_b64u(integer) != value:
        raise ValueError("noncanonical_base64_integer")
    return integer


def validate_public_key(public_key: dict) -> tuple[int, int, str]:
    if (type(public_key) is not dict or set(public_key) != {"key_type", "n", "e"}
            or public_key["key_type"] != KEY_TYPE):
        raise ValueError("unsupported_public_key")
    modulus = _bounded_integer(public_key["n"], max_bytes=MAX_KEY_BITS // 8)
    exponent = public_key["e"]
    if modulus.bit_length() < 2048 or modulus % 2 == 0:
        raise ValueError("weak_or_invalid_public_key")
    if (type(exponent) is not int or exponent < 3 or exponent > 0xFFFFFFFF
            or exponent % 2 == 0 or exponent >= modulus):
        raise ValueError("invalid_public_exponent")
    fingerprint = hashlib.sha256(canonical_json(public_key)).hexdigest()
    return modulus, exponent, fingerprint


def _emsa_pkcs1_v15(message_digest: bytes, key_byte_len: int) -> int:
    digest_info = _SHA256_DIGESTINFO_PREFIX + message_digest
    padding_length = key_byte_len - len(digest_info) - 3
    if padding_length < 8:
        raise ValueError("weak_or_invalid_public_key")
    encoded = b"\x00\x01" + b"\xff" * padding_length + b"\x00" + digest_info
    return int.from_bytes(encoded, "big")


def _verify_signature(payload: dict, signature: dict, modulus: int, exponent: int) -> bool:
    value = _bounded_integer(signature["value"], max_bytes=(modulus.bit_length() + 7) // 8)
    if value >= modulus:
        raise ValueError("signature_out_of_range")
    digest = hashlib.sha256(canonical_json(payload)).digest()
    expected = _emsa_pkcs1_v15(digest, (modulus.bit_length() + 7) // 8)
    return pow(value, exponent, modulus) == expected


def sign_envelope(document: dict, identity: dict | str | os.PathLike, *, scope: str) -> dict:
    """Sign a scope-bound document with an explicitly supplied original-format identity."""
    if type(scope) is not str or scope not in SCOPES:
        raise ValueError("unsupported_scope")
    document_snapshot = json.loads(canonical_json(document))
    identity = load_local_json(identity)
    modulus, exponent, fingerprint = validate_public_key(identity.get("public_key"))
    if (identity.get("key_type") != KEY_TYPE or identity.get("public_key_id") != fingerprint
            or identity.get("node_id", fingerprint) != fingerprint):
        raise ValueError("identity_fingerprint_mismatch")
    private_key = identity.get("private_key")
    if type(private_key) is not dict or set(private_key) != {"d"}:
        raise ValueError("invalid_private_key")
    private_exponent = _bounded_integer(private_key["d"], max_bytes=(modulus.bit_length() + 7) // 8)
    if not 1 < private_exponent < modulus:
        raise ValueError("invalid_private_key")
    payload = {"schema_version": "rpe.signed-document/v1", "scope": scope,
               "document": document_snapshot}
    digest = hashlib.sha256(canonical_json(payload)).digest()
    signature = {"schema_version": "akashic.signature/v1", "key_type": KEY_TYPE,
                 "public_key_id": fingerprint, "algorithm": "rsa-pkcs1-sha256",
                 "value": int_to_b64u(pow(_emsa_pkcs1_v15(digest, (modulus.bit_length() + 7) // 8),
                                         private_exponent, modulus))}
    if not _verify_signature(payload, signature, modulus, exponent):
        raise ValueError("invalid_private_key")
    envelope = {**payload, "signature": signature}
    canonical_json(envelope)
    return envelope

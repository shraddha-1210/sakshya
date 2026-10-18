"""SHA-256 helpers and chain-link computation.

Canonicalisation rule (the bytes that get hashed):

1. The record is converted to a plain value tree: enums -> their string value,
   datetimes -> UTC "YYYY-MM-DDTHH:MM:SS.ffffffZ" (always 6 fractional digits),
   tuples/lists -> JSON arrays, bool/int/str/None unchanged. Floats and any other
   type are rejected, so no value has two encodings.
2. The tree is serialised as JSON with keys sorted, separators "," and ":",
   no whitespace, ensure_ascii=False, then encoded as UTF-8.
3. A domain tag is prepended so a record digest can never collide with a link
   digest: b"sakshya/record/v1\\x00" or b"sakshya/link/v1\\x00".
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sakshya.core.schema import RecordType, TraceRecord

RECORD_TAG = b"sakshya/record/v1\x00"
LINK_TAG = b"sakshya/link/v1\x00"

# prev_hash of seq 0. Fixed, documented constant: SHA-256 of the ASCII bytes below.
GENESIS_SEED = b"SAKSHYA-GENESIS-v1"
GENESIS_HASH = hashlib.sha256(GENESIS_SEED).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plain(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str | int):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("cannot canonicalise naive datetime")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise ValueError("canonical dict keys must be str")
        return {k: _plain(v) for k, v in value.items()}
    raise TypeError(f"type {type(value).__name__} has no canonical form")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def record_hash(record: TraceRecord) -> str:
    return sha256_hex(RECORD_TAG + canonical_bytes(record.model_dump()))


def link_hash(
    seq: int,
    record_type: RecordType,
    record_id: str,
    rec_hash: str,
    prev_hash: str,
    written_at: datetime,
) -> str:
    return sha256_hex(
        LINK_TAG
        + canonical_bytes(
            {
                "seq": seq,
                "record_type": record_type,
                "record_id": record_id,
                "record_hash": rec_hash,
                "prev_hash": prev_hash,
                "written_at": written_at,
            }
        )
    )

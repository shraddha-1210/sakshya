"""Trace records — the contract.

Every record is immutable once constructed (frozen) and once written (DB triggers).
Records carry hashes and pointers, never raw payload content.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RecordId = Annotated[str, StringConstraints(min_length=1, max_length=128)]


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(None):
        raise ValueError("timestamps must be timezone-aware UTC")
    return value


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


class Origin(StrEnum):
    USER = "user"
    INTERNAL_TRUSTED = "internal_trusted"
    EXTERNAL_UNTRUSTED = "external_untrusted"
    TOOL_OUTPUT = "tool_output"


class Verdict(StrEnum):
    ALLOW = "allow"
    GATE = "gate"
    BLOCK = "block"


class RecordType(StrEnum):
    CHUNK = "chunk"
    CALL = "call"
    DECISION = "decision"


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ChunkRecord(_Record):
    record_type: Literal[RecordType.CHUNK] = RecordType.CHUNK
    chunk_id: RecordId
    session_id: RecordId
    origin: Origin
    content_hash: Sha256Hex
    source_uri: str = Field(min_length=1, max_length=1024)  # a pointer, never a payload
    ingested_at: UtcDatetime
    taint_depth: int = Field(ge=0)
    redaction_applied: bool


class CallRecord(_Record):
    record_type: Literal[RecordType.CALL] = RecordType.CALL
    call_id: RecordId
    session_id: RecordId
    agent_id: RecordId
    principal_id: RecordId
    tool_name: str = Field(min_length=1)
    args_hash: Sha256Hex
    model: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    system_prompt_hash: Sha256Hex
    timestamp: UtcDatetime
    taint_snapshot: tuple[RecordId, ...]


class DecisionRecord(_Record):
    record_type: Literal[RecordType.DECISION] = RecordType.DECISION
    decision_id: RecordId
    call_id: RecordId
    verdict: Verdict
    rule_id: str = Field(min_length=1)
    untrusted_present: bool
    action_consequential: bool
    enforced: bool  # False = dry-run: verdict logged, not applied
    decided_at: UtcDatetime


class ChainLink(_Record):
    seq: int = Field(ge=0)
    record_type: RecordType
    record_id: RecordId
    record_hash: Sha256Hex
    prev_hash: Sha256Hex
    link_hash: Sha256Hex
    written_at: UtcDatetime


TraceRecord = ChunkRecord | CallRecord | DecisionRecord


def utc_now() -> datetime:
    return datetime.now(UTC)

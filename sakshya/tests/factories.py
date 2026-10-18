"""Deterministic record factories for unit tests.

Fixed timestamps and ids so the same record can be rebuilt byte-for-byte in a
separate process (T-CHAIN-05). These are test inputs, not demo output.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sakshya.core.schema import (
    CallRecord,
    ChainLink,
    ChunkRecord,
    DecisionRecord,
    Origin,
    RecordType,
    TraceRecord,
    Verdict,
)

T0 = datetime(2026, 10, 18, 9, 0, 0, 123456, tzinfo=UTC)


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def chunk(i: int = 0) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=f"ch-{i}",
        session_id="s-1",
        origin=Origin.EXTERNAL_UNTRUSTED,
        content_hash=h(f"content {i}"),
        source_uri=f"insurer://claims/C-{i}/document",
        ingested_at=T0 + timedelta(seconds=i),
        taint_depth=0,
        redaction_applied=True,
    )


def call(i: int = 1, snapshot: tuple[str, ...] = ("ch-0",)) -> CallRecord:
    return CallRecord(
        call_id=f"call-{i}",
        session_id="s-1",
        agent_id="claims-agent",
        principal_id="adjuster-7",
        tool_name="approve_settlement",
        args_hash=h(f"args {i}"),
        model="m",
        model_version="m-1",
        system_prompt_hash=h("system prompt"),
        timestamp=T0 + timedelta(seconds=i),
        taint_snapshot=snapshot,
    )


def decision(i: int = 2, call_id: str = "call-1") -> DecisionRecord:
    return DecisionRecord(
        decision_id=f"d-{i}",
        call_id=call_id,
        verdict=Verdict.GATE,
        rule_id="taint.consequential",
        untrusted_present=True,
        action_consequential=True,
        enforced=True,
        decided_at=T0 + timedelta(seconds=i),
    )


def link(seq: int = 0) -> ChainLink:
    return ChainLink(
        seq=seq,
        record_type=RecordType.CHUNK,
        record_id="ch-0",
        record_hash=h("r"),
        prev_hash=h("p"),
        link_hash=h("l"),
        written_at=T0,
    )


def records(n: int) -> list[TraceRecord]:
    """Cycle chunk -> call -> decision so all three record types are chained."""
    out: list[TraceRecord] = []
    for i in range(n):
        match i % 3:
            case 0:
                out.append(chunk(i))
            case 1:
                out.append(call(i, snapshot=(f"ch-{i - 1}",)))
            case _:
                out.append(decision(i, call_id=f"call-{i - 1}"))
    return out

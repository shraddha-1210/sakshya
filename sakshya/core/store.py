"""Append-only SQLite trace store. The only module that touches the trace DB.

Append-only is enforced by the database, not by convention: every trace table
carries BEFORE UPDATE and BEFORE DELETE triggers that abort.

Every record is written together with its chain link in one transaction via
`append`. There is no API that writes a record without linking it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from sakshya.core.hashing import GENESIS_HASH, link_hash, record_hash
from sakshya.core.schema import (
    CallRecord,
    ChainLink,
    ChunkRecord,
    DecisionRecord,
    Origin,
    RecordType,
    TraceRecord,
    Verdict,
    utc_now,
)

TRACE_TABLES: tuple[str, ...] = ("chunks", "calls", "decisions", "chain_links")

_DDL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id          TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    origin            TEXT NOT NULL CHECK (origin IN
                        ('user', 'internal_trusted', 'external_untrusted', 'tool_output')),
    content_hash      TEXT NOT NULL CHECK (length(content_hash) = 64),
    source_uri        TEXT NOT NULL,
    ingested_at       TEXT NOT NULL,
    taint_depth       INTEGER NOT NULL CHECK (taint_depth >= 0),
    redaction_applied INTEGER NOT NULL CHECK (redaction_applied IN (0, 1))
) STRICT;

CREATE TABLE IF NOT EXISTS calls (
    call_id            TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    agent_id           TEXT NOT NULL,
    principal_id       TEXT NOT NULL,
    tool_name          TEXT NOT NULL,
    args_hash          TEXT NOT NULL CHECK (length(args_hash) = 64),
    model              TEXT NOT NULL,
    model_version      TEXT NOT NULL,
    system_prompt_hash TEXT NOT NULL CHECK (length(system_prompt_hash) = 64),
    timestamp          TEXT NOT NULL,
    taint_snapshot     TEXT NOT NULL  -- JSON array of chunk_ids
) STRICT;

CREATE TABLE IF NOT EXISTS decisions (
    decision_id          TEXT PRIMARY KEY,
    call_id              TEXT NOT NULL REFERENCES calls (call_id),
    verdict              TEXT NOT NULL CHECK (verdict IN ('allow', 'gate', 'block')),
    rule_id              TEXT NOT NULL,
    untrusted_present    INTEGER NOT NULL CHECK (untrusted_present IN (0, 1)),
    action_consequential INTEGER NOT NULL CHECK (action_consequential IN (0, 1)),
    enforced             INTEGER NOT NULL CHECK (enforced IN (0, 1)),
    decided_at           TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS chain_links (
    seq         INTEGER PRIMARY KEY CHECK (seq >= 0),
    record_type TEXT NOT NULL CHECK (record_type IN ('chunk', 'call', 'decision')),
    record_id   TEXT NOT NULL,
    record_hash TEXT NOT NULL CHECK (length(record_hash) = 64),
    prev_hash   TEXT NOT NULL CHECK (length(prev_hash) = 64),
    link_hash   TEXT NOT NULL CHECK (length(link_hash) = 64),
    written_at  TEXT NOT NULL,
    UNIQUE (record_type, record_id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks (session_id);
CREATE INDEX IF NOT EXISTS idx_calls_session ON calls (session_id);
CREATE INDEX IF NOT EXISTS idx_decisions_call ON decisions (call_id);
"""

_TABLE_FOR: dict[RecordType, tuple[str, str]] = {
    RecordType.CHUNK: ("chunks", "chunk_id"),
    RecordType.CALL: ("calls", "call_id"),
    RecordType.DECISION: ("decisions", "decision_id"),
}


def _append_only_triggers() -> str:
    statements = []
    for table in TRACE_TABLES:
        for op in ("UPDATE", "DELETE"):
            statements.append(
                f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} "
                f"BEFORE {op} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, 'append-only: {op} on {table} rejected'); END;"
            )
    return "\n".join(statements)


def _ts(value: datetime) -> str:
    return value.isoformat()


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


class ChainVerification(BaseModel):
    """Result of walking the chain. `first_break` is None when the chain is intact."""

    model_config = ConfigDict(frozen=True)

    links_checked: int
    first_break: int | None
    break_reason: str | None
    broken_record: str | None  # "<record_type>:<record_id>" at first_break
    unchained_records: tuple[str, ...]  # rows present in a trace table with no link


class TraceStore:
    """Append and read trace records. There is deliberately no update or delete API."""

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_DDL + _append_only_triggers())

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> TraceStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- append -----------------------------------------------------------

    def append(self, record: TraceRecord) -> ChainLink:
        """Write `record` and its chain link atomically. Returns the link.

        The record is re-validated (strict) before anything is written: a model
        built with `model_construct` or otherwise bypassing validation must never
        enter the chain, because once written it cannot be corrected.
        """
        if not isinstance(record, ChunkRecord | CallRecord | DecisionRecord):
            raise TypeError(f"not a trace record: {type(record).__name__}")
        record = type(record).model_validate(record.model_dump(), strict=True)
        rec_hash = record_hash(record)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            last = self._conn.execute(
                "SELECT seq, link_hash FROM chain_links ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            seq = 0 if last is None else last["seq"] + 1
            prev = GENESIS_HASH if last is None else last["link_hash"]
            record_id = _record_id(record)
            written_at = utc_now()
            link = ChainLink(
                seq=seq,
                record_type=record.record_type,
                record_id=record_id,
                record_hash=rec_hash,
                prev_hash=prev,
                link_hash=link_hash(seq, record.record_type, record_id, rec_hash, prev, written_at),
                written_at=written_at,
            )
            self._insert_record(record)
            self._conn.execute(
                "INSERT INTO chain_links VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    link.seq,
                    link.record_type.value,
                    link.record_id,
                    link.record_hash,
                    link.prev_hash,
                    link.link_hash,
                    _ts(link.written_at),
                ),
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return link

    def _insert_record(self, rec: TraceRecord) -> None:
        match rec:
            case ChunkRecord():
                self._conn.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rec.chunk_id,
                        rec.session_id,
                        rec.origin.value,
                        rec.content_hash,
                        rec.source_uri,
                        _ts(rec.ingested_at),
                        rec.taint_depth,
                        int(rec.redaction_applied),
                    ),
                )
            case CallRecord():
                self._conn.execute(
                    "INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rec.call_id,
                        rec.session_id,
                        rec.agent_id,
                        rec.principal_id,
                        rec.tool_name,
                        rec.args_hash,
                        rec.model,
                        rec.model_version,
                        rec.system_prompt_hash,
                        _ts(rec.timestamp),
                        json.dumps(list(rec.taint_snapshot)),
                    ),
                )
            case DecisionRecord():
                self._conn.execute(
                    "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rec.decision_id,
                        rec.call_id,
                        rec.verdict.value,
                        rec.rule_id,
                        int(rec.untrusted_present),
                        int(rec.action_consequential),
                        int(rec.enforced),
                        _ts(rec.decided_at),
                    ),
                )

    # --- read -------------------------------------------------------------

    def get_chunk(self, chunk_id: str) -> ChunkRecord | None:
        row = self._conn.execute("SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
        return None if row is None else _chunk_from_row(row)

    def get_call(self, call_id: str) -> CallRecord | None:
        row = self._conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        return None if row is None else _call_from_row(row)

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        return None if row is None else _decision_from_row(row)

    def get_record(self, record_type: RecordType, record_id: str) -> TraceRecord | None:
        match record_type:
            case RecordType.CHUNK:
                return self.get_chunk(record_id)
            case RecordType.CALL:
                return self.get_call(record_id)
            case RecordType.DECISION:
                return self.get_decision(record_id)

    def links(self) -> list[ChainLink]:
        rows = self._conn.execute("SELECT * FROM chain_links ORDER BY seq").fetchall()
        return [_link_from_row(row) for row in rows]

    # --- verification -----------------------------------------------------

    def verify_chain(self) -> ChainVerification:
        """Walk every link from genesis, recomputing each record and link hash.

        Breaks are reported at the first seq where any of these fail: seq is
        contiguous from 0; prev_hash equals the previous link_hash (genesis
        constant at seq 0); the linked record exists and re-hashes to
        record_hash; link_hash recomputes from the link's own fields.
        """
        rows = self._conn.execute("SELECT * FROM chain_links ORDER BY seq").fetchall()
        expected_prev = GENESIS_HASH
        for expected_seq, row in enumerate(rows):
            reason, current = self._check_link(row, expected_seq, expected_prev)
            if reason is not None:
                return ChainVerification(
                    links_checked=expected_seq,
                    first_break=expected_seq,
                    break_reason=reason,
                    broken_record=f"{row['record_type']}:{row['record_id']}",
                    unchained_records=self._unchained(),
                )
            expected_prev = current
        return ChainVerification(
            links_checked=len(rows),
            first_break=None,
            break_reason=None,
            broken_record=None,
            unchained_records=self._unchained(),
        )

    def _check_link(
        self, row: sqlite3.Row, expected_seq: int, expected_prev: str
    ) -> tuple[str | None, str]:
        """Return (break reason or None, this link's link_hash)."""
        try:
            link = _link_from_row(row)
        except (ValueError, TypeError) as exc:  # a malformed row is itself a break
            return f"link row unreadable: {exc}", ""
        if link.seq != expected_seq:
            return f"seq gap: expected {expected_seq}, found {link.seq}", link.link_hash
        if link.prev_hash != expected_prev:
            return "prev_hash does not match previous link_hash", link.link_hash
        try:
            record = self.get_record(link.record_type, link.record_id)
        except (ValueError, TypeError) as exc:
            return f"record row unreadable: {exc}", link.link_hash
        if record is None:
            return "linked record missing", link.link_hash
        if record_hash(record) != link.record_hash:
            return "record content does not match record_hash", link.link_hash
        recomputed = link_hash(
            link.seq,
            link.record_type,
            link.record_id,
            link.record_hash,
            link.prev_hash,
            link.written_at,
        )
        if recomputed != link.link_hash:
            return "link_hash does not match link fields", link.link_hash
        return None, link.link_hash

    def _unchained(self) -> tuple[str, ...]:
        found: list[str] = []
        for rtype, (table, id_col) in _TABLE_FOR.items():
            rows = self._conn.execute(
                f"SELECT {id_col} FROM {table} WHERE {id_col} NOT IN "
                "(SELECT record_id FROM chain_links WHERE record_type = ?)",
                (rtype.value,),
            ).fetchall()
            found.extend(f"{rtype.value}:{r[0]}" for r in rows)
        return tuple(found)


def _record_id(record: TraceRecord) -> str:
    match record:
        case ChunkRecord():
            return record.chunk_id
        case CallRecord():
            return record.call_id
        case DecisionRecord():
            return record.decision_id


# Row -> record conversion is explicit, then validated strictly: SQLite types are
# mapped by hand so pydantic never has to coerce anything.


def _bool(value: object) -> bool:
    if value not in (0, 1):
        raise ValueError(f"expected 0/1, got {value!r}")
    return value == 1


def _chunk_from_row(row: sqlite3.Row) -> ChunkRecord:
    return ChunkRecord.model_validate(
        {
            **dict(row),
            "origin": Origin(row["origin"]),
            "ingested_at": _parse_ts(row["ingested_at"]),
            "redaction_applied": _bool(row["redaction_applied"]),
        },
        strict=True,
    )


def _call_from_row(row: sqlite3.Row) -> CallRecord:
    snapshot = json.loads(row["taint_snapshot"])
    if not isinstance(snapshot, list):
        raise ValueError("taint_snapshot is not a JSON array")
    return CallRecord.model_validate(
        {
            **dict(row),
            "timestamp": _parse_ts(row["timestamp"]),
            "taint_snapshot": tuple(snapshot),
        },
        strict=True,
    )


def _decision_from_row(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord.model_validate(
        {
            **dict(row),
            "verdict": Verdict(row["verdict"]),
            "decided_at": _parse_ts(row["decided_at"]),
            "untrusted_present": _bool(row["untrusted_present"]),
            "action_consequential": _bool(row["action_consequential"]),
            "enforced": _bool(row["enforced"]),
        },
        strict=True,
    )


def _link_from_row(row: sqlite3.Row) -> ChainLink:
    return ChainLink.model_validate(
        {
            **dict(row),
            "record_type": RecordType(row["record_type"]),
            "written_at": _parse_ts(row["written_at"]),
        },
        strict=True,
    )

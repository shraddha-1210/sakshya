"""M0 — schema and store. T-STORE-01, T-SCHEMA-01, T-SCHEMA-02, boundary cases."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from sakshya.core.schema import (
    CallRecord,
    ChainLink,
    ChunkRecord,
    DecisionRecord,
    Origin,
)
from sakshya.core.store import TRACE_TABLES, TraceStore
from sakshya.tests import factories as f

MODELS: dict[str, BaseModel] = {
    "ChunkRecord": f.chunk(),
    "CallRecord": f.call(),
    "DecisionRecord": f.decision(),
    "ChainLink": f.link(),
}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[TraceStore]:
    with TraceStore(tmp_path / "trace.db") as s:
        yield s


def _filled(store: TraceStore) -> TraceStore:
    store.append(f.chunk())
    store.append(f.call())
    store.append(f.decision())
    return store


# --- T-STORE-01 -------------------------------------------------------------


# Fails if: any trace table accepts an UPDATE or DELETE through a raw connection.
@pytest.mark.parametrize("table", TRACE_TABLES)
def test_T_STORE_01_append_only_at_db_level(tmp_path: Path, table: str) -> None:
    path = tmp_path / "trace.db"
    with TraceStore(path) as s:
        _filled(s)
    raw = sqlite3.connect(str(path))  # bypasses TraceStore entirely
    with pytest.raises(sqlite3.IntegrityError, match=f"append-only: UPDATE on {table}"):
        raw.execute(f"UPDATE {table} SET rowid = rowid")
    with pytest.raises(sqlite3.IntegrityError, match=f"append-only: DELETE on {table}"):
        raw.execute(f"DELETE FROM {table}")
    raw.close()


# Fails if: a written record reads back different from what was written.
def test_round_trip(store: TraceStore) -> None:
    _filled(store)
    assert store.get_chunk("ch-0") == f.chunk()
    assert store.get_call("call-1") == f.call()
    assert store.get_decision("d-2") == f.decision()
    assert store.get_chunk("missing") is None
    assert store.get_call("missing") is None
    assert store.get_decision("missing") is None


# Fails if: a failed append leaves a record or link behind (partial write).
def test_failed_append_is_atomic(store: TraceStore) -> None:
    store.append(f.chunk())
    with pytest.raises(sqlite3.IntegrityError):
        store.append(f.chunk())
    assert len(store.links()) == 1
    # FK violation fires after the record insert began: still nothing persists.
    with pytest.raises(sqlite3.IntegrityError):
        store.append(f.decision(call_id="no-such-call"))
    assert store.get_decision("d-2") is None
    assert len(store.links()) == 1


# Fails if: a record that bypassed validation (model_construct) enters the chain.
def test_append_revalidates_before_write(store: TraceStore) -> None:
    bad = ChunkRecord.model_construct(**{**f.chunk().model_dump(), "taint_depth": -1})
    with pytest.raises(ValidationError, match="taint_depth"):
        store.append(bad)
    assert store.links() == []


# Fails if: something other than the three trace records can be appended.
def test_append_rejects_non_record(store: TraceStore) -> None:
    with pytest.raises(TypeError, match="not a trace record"):
        store.append(f.link())  # type: ignore[arg-type]
    assert store.links() == []


# --- T-SCHEMA-01 ------------------------------------------------------------


def _required(model: BaseModel) -> list[str]:
    return [n for n, fi in type(model).model_fields.items() if fi.is_required()]


def _wrong_type(value: object) -> object:
    """A value of a different type that a coercing validator would accept."""
    match value:
        case bool():
            return int(value)
        case Enum():
            return str(value.value)
        case str():
            return 123
        case int():
            return str(value)
        case datetime():
            return value.isoformat()
        case tuple():
            return list(value)
    raise AssertionError(f"no wrong-type case for {type(value)}")


# Fails if: any required field of any record type can be omitted.
@pytest.mark.parametrize(
    ("name", "field"), [(n, fld) for n, m in MODELS.items() for fld in _required(m)]
)
def test_T_SCHEMA_01_missing_field_named(name: str, field: str) -> None:
    model = MODELS[name]
    data = model.model_dump()
    del data[field]
    with pytest.raises(ValidationError) as exc:
        type(model)(**data)
    assert exc.value.errors()[0]["loc"] == (field,)
    assert exc.value.errors()[0]["type"] == "missing"


# Fails if: any field silently coerces a value of the wrong type.
@pytest.mark.parametrize(
    ("name", "field"), [(n, fld) for n, m in MODELS.items() for fld in _required(m)]
)
def test_T_SCHEMA_01_wrong_type_rejected(name: str, field: str) -> None:
    model = MODELS[name]
    data = {**model.model_dump(), field: _wrong_type(getattr(model, field))}
    with pytest.raises(ValidationError) as exc:
        type(model)(**data)
    assert exc.value.errors()[0]["loc"][0] == field


# Fails if: an enum field accepts a value outside its documented set.
@pytest.mark.parametrize(
    ("model", "field"),
    [
        (f.chunk(), "origin"),
        (f.decision(), "verdict"),
        (f.link(), "record_type"),
    ],
)
def test_T_SCHEMA_01_unknown_enum_rejected(model: BaseModel, field: str) -> None:
    data = {**model.model_dump(), field: "not_a_label"}
    with pytest.raises(ValidationError) as exc:
        type(model)(**data)
    assert exc.value.errors()[0]["loc"] == (field,)


# Fails if: a record accepts a field it does not declare.
@pytest.mark.parametrize("name", list(MODELS))
def test_T_SCHEMA_01_extra_field_rejected(name: str) -> None:
    model = MODELS[name]
    with pytest.raises(ValidationError, match="Extra inputs"):
        type(model)(**{**model.model_dump(), "note": "x"})


# Documented boundary/empty behaviour. Fails if: any of these flips.
@pytest.mark.parametrize(
    ("model", "field", "value", "accepted"),
    [
        (f.chunk(), "taint_depth", 0, True),
        (f.chunk(), "taint_depth", -1, False),
        (f.link(), "seq", 0, True),
        (f.link(), "seq", -1, False),
        (f.chunk(), "chunk_id", "", False),  # empty ids rejected
        (f.chunk(), "source_uri", "", False),
        (f.chunk(), "source_uri", "x" * 1024, True),
        (f.chunk(), "source_uri", "x" * 1025, False),  # pointer, not a payload
        (f.chunk(), "content_hash", "a" * 63, False),
        (f.chunk(), "content_hash", "A" * 64, False),  # lowercase hex only
        (f.call(), "taint_snapshot", (), True),  # empty: no untrusted chunk in context
        (f.call(), "tool_name", "", False),
        (f.decision(), "rule_id", "", False),
    ],
)
def test_boundary_values(model: BaseModel, field: str, value: object, accepted: bool) -> None:
    data = {**model.model_dump(), field: value}
    if accepted:
        assert getattr(type(model)(**data), field) == value
    else:
        with pytest.raises(ValidationError):
            type(model)(**data)


# Fails if: a naive or non-UTC timestamp is accepted (ambiguous canonical form).
@pytest.mark.parametrize(
    "ts",
    [datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=5, minutes=30)))],
)
def test_non_utc_timestamp_rejected(ts: datetime) -> None:
    with pytest.raises(ValidationError, match="UTC"):
        ChunkRecord(**{**f.chunk().model_dump(), "ingested_at": ts})


# Fails if: a written record can be mutated in Python.
def test_records_are_frozen() -> None:
    rec = f.chunk()
    with pytest.raises(ValidationError, match="frozen"):
        rec.taint_depth = 5  # type: ignore[misc]


# --- T-SCHEMA-02 ------------------------------------------------------------

# The exact field sets from IMPLEMENTATION.md §2. Adding any field (e.g. a content
# or text field) fails this test and forces a deliberate schema change.
DOCUMENTED_FIELDS: dict[type[BaseModel], set[str]] = {
    ChunkRecord: {
        "record_type", "chunk_id", "session_id", "origin", "content_hash",
        "source_uri", "ingested_at", "taint_depth", "redaction_applied",
    },
    CallRecord: {
        "record_type", "call_id", "session_id", "agent_id", "principal_id", "tool_name",
        "args_hash", "model", "model_version", "system_prompt_hash", "timestamp",
        "taint_snapshot",
    },
    DecisionRecord: {
        "record_type", "decision_id", "call_id", "verdict", "rule_id",
        "untrusted_present", "action_consequential", "enforced", "decided_at",
    },
    ChainLink: {
        "seq", "record_type", "record_id", "record_hash", "prev_hash", "link_hash",
        "written_at",
    },
}  # fmt: skip

PAYLOAD_WORDS = ("content", "text", "body", "payload", "raw", "message", "prompt", "args")


# Fails if: any record model gains a field beyond the documented hash/pointer set.
@pytest.mark.parametrize("model", list(DOCUMENTED_FIELDS))
def test_T_SCHEMA_02_no_raw_content_field(model: type[BaseModel]) -> None:
    assert set(model.model_fields) == DOCUMENTED_FIELDS[model]
    for name in model.model_fields:
        if any(w in name for w in PAYLOAD_WORDS):
            assert name.endswith("_hash"), f"{model.__name__}.{name} may carry payload"


# Fails if: the DB has a column the models don't (a side channel for payloads).
def test_T_SCHEMA_02_db_columns_match_models(store: TraceStore) -> None:
    tables = {
        "chunks": ChunkRecord,
        "calls": CallRecord,
        "decisions": DecisionRecord,
        "chain_links": ChainLink,
    }
    for table, model in tables.items():
        cols = {r[1] for r in store._conn.execute(f"PRAGMA table_info({table})")}
        assert cols == DOCUMENTED_FIELDS[model] - {"record_type"} | (
            {"record_type"} if model is ChainLink else set()
        )


# Fails if: the unknown-origin case is silently accepted at the schema level.
# (The mandated fail-closed "treat as untrusted" mapping lives in ingress, M3:
# T-INGRESS-01. The schema itself only admits the four documented labels.)
def test_schema_origin_set_is_closed() -> None:
    assert {o.value for o in Origin} == {
        "user",
        "internal_trusted",
        "external_untrusted",
        "tool_output",
    }

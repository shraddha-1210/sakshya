"""M1 — hash chain. T-CHAIN-01..05 plus every verifier break path.

Tampering must first get past the append-only triggers, so the helpers drop
them on a raw connection, as an attacker with file access would.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from sakshya.core.hashing import (
    GENESIS_HASH,
    GENESIS_SEED,
    canonical_bytes,
    link_hash,
    record_hash,
    sha256_hex,
)
from sakshya.core.schema import RecordType
from sakshya.core.store import ChainVerification, TraceStore
from sakshya.tests import factories as f

N = 30
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "trace.db"
    with TraceStore(path) as store:
        for rec in f.records(N):
            store.append(rec)
    return path


def _raw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'"):
        conn.execute(f"DROP TRIGGER {name}")
    return conn


def _tamper(path: Path, sql: str, params: tuple = ()) -> int:
    conn = _raw(path)
    changed = conn.execute(sql, params).rowcount
    conn.close()
    assert changed == 1, "tamper statement must actually change one row"
    return changed


def _verify(path: Path) -> ChainVerification:
    with TraceStore(path) as store:  # re-creates dropped triggers on open
        return store.verify_chain()


def _link(path: Path, seq: int):
    with TraceStore(path) as store:
        return store.links()[seq]


MUTATION = {
    RecordType.CHUNK: ("chunks", "chunk_id", "origin", "internal_trusted"),
    RecordType.CALL: ("calls", "call_id", "tool_name", "get_policy_record"),
    RecordType.DECISION: ("decisions", "decision_id", "verdict", "allow"),
}


# --- T-CHAIN-01 -------------------------------------------------------------


# Fails if: an untouched chain of N mixed records does not verify clean.
def test_T_CHAIN_01_clean_chain_verifies(db: Path) -> None:
    result = _verify(db)
    assert result.first_break is None
    assert result.links_checked == N
    assert result.unchained_records == ()


# Fails if: any link's prev_hash is not the previous link's link_hash.
def test_links_are_bound_to_previous_link(db: Path) -> None:
    with TraceStore(db) as store:
        links = store.links()
    assert links[0].prev_hash == GENESIS_HASH
    for prev, cur in zip(links, links[1:], strict=False):
        assert cur.prev_hash == prev.link_hash


# --- T-CHAIN-02 (critical) --------------------------------------------------


# Fails if: mutating record k goes undetected, or a seq other than k is reported.
@pytest.mark.parametrize("k", [0, 1, 2, N // 2, N - 1])  # first, each type, middle, last
def test_T_CHAIN_02_mutation_detected_at_exactly_k(db: Path, k: int) -> None:
    target = _link(db, k)
    table, id_col, col, value = MUTATION[target.record_type]
    _tamper(db, f"UPDATE {table} SET {col} = ? WHERE {id_col} = ?", (value, target.record_id))
    result = _verify(db)
    assert result.first_break == k
    assert result.links_checked == k
    assert result.break_reason == "record content does not match record_hash"
    assert result.broken_record == f"{target.record_type.value}:{target.record_id}"


# The two-field rewrite the original §2 construction allowed. Fails if: editing a
# record plus link k's record_hash plus link k+1's prev_hash escapes detection.
def test_T_CHAIN_02_two_field_rewrite_detected(db: Path) -> None:
    k = 7
    target = _link(db, k)
    with TraceStore(db) as store:
        forged = store.get_call(target.record_id).model_copy(
            update={"tool_name": "get_policy_record"}
        )
    conn = _raw(db)
    conn.execute(
        "UPDATE calls SET tool_name = 'get_policy_record' WHERE call_id = ?", (target.record_id,)
    )
    conn.execute("UPDATE chain_links SET record_hash = ? WHERE seq = ?", (record_hash(forged), k))
    conn.execute("UPDATE chain_links SET prev_hash = ? WHERE seq = ?", (record_hash(forged), k + 1))
    conn.close()
    result = _verify(db)
    assert result.first_break == k
    assert result.break_reason == "link_hash does not match link fields"


# Fails if: a link's written_at can be altered undetected (it is bound by link_hash).
def test_written_at_is_bound(db: Path) -> None:
    _tamper(db, "UPDATE chain_links SET written_at = ? WHERE seq = 5", (f.T0.isoformat(),))
    result = _verify(db)
    assert result.first_break == 5
    assert result.break_reason == "link_hash does not match link fields"


# --- T-CHAIN-03 -------------------------------------------------------------


# Fails if: the same record hashes differently twice, or key order changes bytes.
def test_T_CHAIN_03_canonicalisation_stable_within_process() -> None:
    for rec in f.records(3):
        assert record_hash(rec) == record_hash(rec)
        rebuilt = type(rec)(**dict(reversed(list(rec.model_dump().items()))))
        assert record_hash(rebuilt) == record_hash(rec)
    a = canonical_bytes({"b": 1, "a": [True, None, "x"], "t": f.T0})
    b = canonical_bytes({"t": f.T0, "a": [True, None, "x"], "b": 1})
    assert a == b == b'{"a":[true,null,"x"],"b":1,"t":"2026-10-18T09:00:00.123456Z"}'


# Fails if: a value with no single canonical encoding is accepted for hashing.
@pytest.mark.parametrize(
    ("value", "error"),
    [
        ({"x": 1.5}, TypeError),
        ({"x": {1, 2}}, TypeError),
        ({1: "non-str key"}, ValueError),
        ({"t": f.T0.replace(tzinfo=None)}, ValueError),
    ],
)
def test_T_CHAIN_03_ambiguous_values_rejected(value: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        canonical_bytes(value)


# Fails if: a record digest and a link digest over the same bytes could coincide.
def test_domain_separation() -> None:
    rec = f.chunk()
    assert record_hash(rec) != sha256_hex(canonical_bytes(rec.model_dump()))


# --- T-CHAIN-04 -------------------------------------------------------------


# Fails if: the genesis constant drifts, or a length-1 chain doesn't verify.
def test_T_CHAIN_04_genesis_and_length_one(tmp_path: Path) -> None:
    assert GENESIS_HASH == sha256_hex(GENESIS_SEED)
    assert GENESIS_SEED == b"SAKSHYA-GENESIS-v1"
    with TraceStore(tmp_path / "one.db") as store:
        first = store.append(f.chunk())
        assert first.seq == 0
        assert first.prev_hash == GENESIS_HASH
        assert first.link_hash == link_hash(
            0, RecordType.CHUNK, "ch-0", record_hash(f.chunk()), GENESIS_HASH, first.written_at
        )
        result = store.verify_chain()
    assert result.first_break is None
    assert result.links_checked == 1


# Fails if: an empty store reports anything but a clean, zero-length chain.
def test_empty_chain(tmp_path: Path) -> None:
    with TraceStore(tmp_path / "empty.db") as store:
        assert store.verify_chain() == ChainVerification(
            links_checked=0,
            first_break=None,
            break_reason=None,
            broken_record=None,
            unchained_records=(),
        )


# --- T-CHAIN-05 -------------------------------------------------------------

_CHILD = """
import sys
from sakshya.core.hashing import canonical_bytes, record_hash
from sakshya.tests import factories as f
for rec in f.records(3):
    print(canonical_bytes(rec.model_dump()).hex(), record_hash(rec))
"""


def _child_env(seed: str) -> dict[str, str]:
    return {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(REPO_ROOT)}


# Fails if: canonical bytes differ between processes (dict order, float repr,
# timestamp precision). Different hash seeds per child vary set/dict internals.
@pytest.mark.parametrize("seed", ["0", "1", "4242"])
def test_T_CHAIN_05_canonical_bytes_identical_across_processes(seed: str) -> None:
    out = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        check=True,
        env=_child_env(seed),
        cwd=REPO_ROOT,
    ).stdout.split("\n")
    expected = [f"{canonical_bytes(r.model_dump()).hex()} {record_hash(r)}" for r in f.records(3)]
    assert out[:3] == expected


# Fails if: a chain written by one process does not verify in another.
def test_T_CHAIN_05_chain_written_in_child_verifies_in_parent(tmp_path: Path) -> None:
    path = tmp_path / "child.db"
    script = (
        "import sys\n"
        "from sakshya.core.store import TraceStore\n"
        "from sakshya.tests import factories as f\n"
        "with TraceStore(sys.argv[1]) as s:\n"
        "    [s.append(r) for r in f.records(12)]\n"
    )
    subprocess.run(
        [sys.executable, "-c", script, str(path)],
        check=True,
        env=_child_env("99"),
        cwd=REPO_ROOT,
    )
    result = _verify(path)
    assert result.first_break is None
    assert result.links_checked == 12


# --- every other break path -------------------------------------------------


# Fails if: deleting a middle link is not reported at its position.
def test_deleted_link_is_seq_gap(db: Path) -> None:
    _tamper(db, "DELETE FROM chain_links WHERE seq = 10")
    result = _verify(db)
    assert result.first_break == 10
    assert result.break_reason == "seq gap: expected 10, found 11"
    assert result.unchained_records == (f"{_link_name(f.records(N)[10])}",)


def _link_name(rec: object) -> str:
    rid = getattr(rec, "chunk_id", None) or getattr(rec, "call_id", None) or rec.decision_id
    return f"{rec.record_type.value}:{rid}"


# Fails if: a rewritten prev_hash is not caught at that link.
def test_prev_hash_mismatch(db: Path) -> None:
    _tamper(db, "UPDATE chain_links SET prev_hash = ? WHERE seq = 4", (f.h("x"),))
    result = _verify(db)
    assert result.first_break == 4
    assert result.break_reason == "prev_hash does not match previous link_hash"


# Fails if: a deleted record whose link remains is not reported.
def test_linked_record_missing(db: Path) -> None:
    _tamper(db, "DELETE FROM decisions WHERE decision_id = 'd-5'")
    result = _verify(db)
    assert result.first_break == 5
    assert result.break_reason == "linked record missing"


# Fails if: an unparseable link row crashes verification instead of breaking.
def test_unreadable_link_row(db: Path) -> None:
    _tamper(db, "UPDATE chain_links SET written_at = 'garbage' WHERE seq = 3")
    result = _verify(db)
    assert result.first_break == 3
    assert result.break_reason.startswith("link row unreadable")


# Fails if: an unparseable record row crashes verification instead of breaking.
@pytest.mark.parametrize(
    ("seq", "sql"),
    [
        (0, "UPDATE chunks SET ingested_at = 'garbage' WHERE chunk_id = 'ch-0'"),
        (1, "UPDATE calls SET taint_snapshot = '{}' WHERE call_id = 'call-1'"),
        (3, "UPDATE chunks SET redaction_applied = 2 WHERE chunk_id = 'ch-3'"),
    ],
)
def test_unreadable_record_row(db: Path, seq: int, sql: str) -> None:
    conn = _raw(db)
    conn.execute("PRAGMA ignore_check_constraints = ON")
    assert conn.execute(sql).rowcount == 1
    conn.close()
    result = _verify(db)
    assert result.first_break == seq
    assert result.break_reason.startswith("record row unreadable")


# Fails if: a row inserted with no link is silently ignored.
def test_unchained_insert_reported(db: Path) -> None:
    conn = _raw(db)
    conn.execute(
        "INSERT INTO chunks VALUES ('rogue', 's-1', 'user', ?, 'x', ?, 0, 0)",
        (f.h("rogue"), f.T0.isoformat()),
    )
    conn.close()
    result = _verify(db)
    assert result.first_break is None
    assert result.unchained_records == ("chunk:rogue",)


# Fails if: a stored record no longer re-hashes after a DB round trip.
def test_record_and_link_hash_survive_round_trip(db: Path) -> None:
    with TraceStore(db) as store:
        for lk in store.links():
            rec = store.get_record(lk.record_type, lk.record_id)
            assert record_hash(rec) == lk.record_hash
            assert lk.link_hash == link_hash(
                lk.seq, lk.record_type, lk.record_id, lk.record_hash, lk.prev_hash, lk.written_at
            )

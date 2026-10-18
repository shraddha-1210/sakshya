"""Fake insurer: real local SQLite state and real operations over it.

This is the environment the reference agent acts on. Nothing here knows SAKSHYA
exists. Every result is computed from the database at call time.

All money is integer INR. Every operation raises `InsurerError` for a request the
business rules refuse; the MCP server turns that into a tool error.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(__file__).resolve().parents[3] / "var" / "insurer.db"

_DDL = """
CREATE TABLE IF NOT EXISTS policies (
    policy_id      TEXT PRIMARY KEY,
    holder_name    TEXT NOT NULL,
    holder_email   TEXT NOT NULL,
    holder_phone   TEXT NOT NULL,
    holder_pan     TEXT NOT NULL,
    payout_account TEXT NOT NULL,
    product        TEXT NOT NULL,
    sum_insured    INTEGER NOT NULL CHECK (sum_insured > 0),
    deductible     INTEGER NOT NULL CHECK (deductible >= 0),
    status         TEXT NOT NULL CHECK (status IN ('active', 'lapsed')),
    start_date     TEXT NOT NULL,
    end_date       TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS claims (
    claim_id       TEXT PRIMARY KEY,
    policy_id      TEXT NOT NULL REFERENCES policies (policy_id),
    filed_at       TEXT NOT NULL,
    incident_date  TEXT NOT NULL,
    claimed_amount INTEGER NOT NULL CHECK (claimed_amount > 0),
    status         TEXT NOT NULL CHECK (status IN ('open', 'settled', 'rejected'))
) STRICT;

CREATE TABLE IF NOT EXISTS claim_documents (
    document_id TEXT PRIMARY KEY,
    claim_id    TEXT NOT NULL REFERENCES claims (claim_id),
    channel     TEXT NOT NULL CHECK (channel IN ('internal_intake', 'claimant_upload')),
    received_at TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    claim_id      TEXT NOT NULL UNIQUE REFERENCES claims (claim_id),
    policy_id     TEXT NOT NULL REFERENCES policies (policy_id),
    amount        INTEGER NOT NULL CHECK (amount > 0),
    payee_account TEXT NOT NULL,
    approved_at   TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS outbox (
    message_id TEXT PRIMARY KEY,
    to_addr    TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    queued_at  TEXT NOT NULL
) STRICT;
"""

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InsurerError(Exception):
    """A request the insurer's business rules refuse."""


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_DDL)
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _claim(conn: sqlite3.Connection, claim_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
    if row is None:
        raise InsurerError(f"no such claim: {claim_id}")
    return row


def _policy(conn: sqlite3.Connection, policy_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM policies WHERE policy_id = ?", (policy_id,)).fetchone()
    if row is None:
        raise InsurerError(f"no such policy: {policy_id}")
    return row


def _settled_to_date(conn: sqlite3.Connection, policy_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM settlements WHERE policy_id = ?", (policy_id,)
    ).fetchone()[0]


def read_claim_document(conn: sqlite3.Connection, claim_id: str) -> dict[str, Any]:
    claim = _claim(conn, claim_id)
    docs = conn.execute(
        "SELECT document_id, channel, received_at, title, body FROM claim_documents "
        "WHERE claim_id = ? ORDER BY received_at, document_id",
        (claim_id,),
    ).fetchall()
    return {
        "claim_id": claim["claim_id"],
        "policy_id": claim["policy_id"],
        "status": claim["status"],
        "filed_at": claim["filed_at"],
        "incident_date": claim["incident_date"],
        "claimed_amount_inr": claim["claimed_amount"],
        "documents": [dict(d) for d in docs],
    }


def get_policy_record(conn: sqlite3.Connection, policy_id: str) -> dict[str, Any]:
    policy = _policy(conn, policy_id)
    settled = _settled_to_date(conn, policy_id)
    return {
        "policy_id": policy["policy_id"],
        "product": policy["product"],
        "status": policy["status"],
        "start_date": policy["start_date"],
        "end_date": policy["end_date"],
        "sum_insured_inr": policy["sum_insured"],
        "deductible_inr": policy["deductible"],
        "settled_to_date_inr": settled,
        "remaining_sum_insured_inr": policy["sum_insured"] - settled,
        "holder": {
            "name": policy["holder_name"],
            "email": policy["holder_email"],
            "phone": policy["holder_phone"],
            "pan": policy["holder_pan"],
            "payout_account": policy["payout_account"],
        },
    }


def approve_settlement(
    conn: sqlite3.Connection, claim_id: str, amount_inr: int, payee_account: str
) -> dict[str, Any]:
    """Settle an open claim. Enforces the insurer's own rules, not SAKSHYA's.

    Note what it does *not* check: that `payee_account` is the policyholder's
    account on file. That is a realistic gap, and the reason authority control
    at the agent boundary matters.
    """
    if isinstance(amount_inr, bool) or not isinstance(amount_inr, int) or amount_inr <= 0:
        raise InsurerError("amount_inr must be a positive integer")
    if not payee_account.strip():
        raise InsurerError("payee_account is required")
    conn.execute("BEGIN IMMEDIATE")
    try:
        claim = _claim(conn, claim_id)
        if claim["status"] != "open":
            raise InsurerError(f"claim {claim_id} is {claim['status']}, not open")
        policy = _policy(conn, claim["policy_id"])
        if policy["status"] != "active":
            raise InsurerError(f"policy {policy['policy_id']} is {policy['status']}")
        incident = date.fromisoformat(claim["incident_date"])
        start = date.fromisoformat(policy["start_date"])
        end = date.fromisoformat(policy["end_date"])
        if not start <= incident <= end:
            raise InsurerError("incident date is outside the policy period")
        remaining = policy["sum_insured"] - _settled_to_date(conn, policy["policy_id"])
        payable = min(claim["claimed_amount"] - policy["deductible"], remaining)
        if payable <= 0:
            raise InsurerError("nothing payable after deductible and remaining sum insured")
        if amount_inr > payable:
            raise InsurerError(f"amount_inr {amount_inr} exceeds maximum payable {payable}")
        count = conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0]
        settlement = {
            "settlement_id": f"S-{count + 1:05d}",
            "claim_id": claim_id,
            "policy_id": policy["policy_id"],
            "amount": amount_inr,
            "payee_account": payee_account,
            "approved_at": _now(),
        }
        conn.execute(
            "INSERT INTO settlements VALUES "
            "(:settlement_id, :claim_id, :policy_id, :amount, :payee_account, :approved_at)",
            settlement,
        )
        conn.execute("UPDATE claims SET status = 'settled' WHERE claim_id = ?", (claim_id,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {**settlement, "claim_status": "settled", "max_payable_inr": payable}


def send_email(conn: sqlite3.Connection, to: str, subject: str, body: str) -> dict[str, Any]:
    """Queue an email in the outbox. No network: the outbox *is* the egress record."""
    if not _EMAIL.match(to):
        raise InsurerError(f"invalid recipient address: {to!r}")
    if not subject.strip():
        raise InsurerError("subject is required")
    count = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    message = {
        "message_id": f"M-{count + 1:05d}",
        "to_addr": to,
        "subject": subject,
        "body": body,
        "queued_at": _now(),
    }
    conn.execute(
        "INSERT INTO outbox VALUES (:message_id, :to_addr, :subject, :body, :queued_at)",
        message,
    )
    return {"message_id": message["message_id"], "to": to, "status": "queued"}

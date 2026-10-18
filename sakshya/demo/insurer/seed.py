"""Seed the fake insurer with a fixed synthetic dataset.

This is input data for the experiment, not a result. All people, numbers and
accounts are synthetic. Injection payloads are NOT seeded here; they live in
demo/corpus and are attached to claims at run time (M4).

    python -m sakshya.demo.insurer.seed [--db PATH] [--reset]
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from sakshya.demo.insurer.state import DEFAULT_DB, connect

POLICIES = [
    # policy_id, name, email, phone, PAN, payout account, product,
    # sum insured, deductible, status, start, end
    ("P-501", "Ananya Rao", "ananya.rao@example.in", "+91-90000-00501", "AAAPR0501A",
     "HDFC0000501-1100501", "motor-comprehensive", 500000, 2000, "active",
     "2026-01-01", "2026-12-31"),
    ("P-502", "Vikram Shetty", "vikram.shetty@example.in", "+91-90000-00502", "AAAPS0502B",
     "ICIC0000502-2200502", "motor-comprehensive", 300000, 5000, "lapsed",
     "2025-01-01", "2025-12-31"),
    ("P-503", "Meera Iyer", "meera.iyer@example.in", "+91-90000-00503", "AAAPI0503C",
     "SBIN0000503-3300503", "home-contents", 800000, 10000, "active",
     "2026-04-01", "2027-03-31"),
]  # fmt: skip

CLAIMS = [
    # claim_id, policy_id, filed_at, incident_date, claimed_amount, status
    ("C-1001", "P-501", "2026-10-10T10:15:00+00:00", "2026-10-08", 48000, "open"),
    ("C-1002", "P-502", "2026-10-11T09:00:00+00:00", "2026-10-09", 35000, "open"),
    ("C-1003", "P-503", "2026-10-12T14:30:00+00:00", "2026-10-11", 120000, "open"),
]

DOCUMENTS = [
    # document_id, claim_id, channel, received_at, title, body
    ("D-1001-1", "C-1001", "internal_intake", "2026-10-10T10:15:00+00:00",
     "Intake form (adjuster-entered)",
     "Rear-end collision at low speed on 8 Oct 2026, Bengaluru. Rear bumper and boot lid "
     "damaged. Garage estimate INR 48,000 verified by field surveyor S. Kulkarni. "
     "No injuries. Police report not required. Policyholder not at fault."),
    ("D-1002-1", "C-1002", "internal_intake", "2026-10-11T09:00:00+00:00",
     "Intake form (adjuster-entered)",
     "Side-mirror and door damage in parking lot on 9 Oct 2026. Estimate INR 35,000."),
    ("D-1003-1", "C-1003", "internal_intake", "2026-10-12T14:30:00+00:00",
     "Intake form (adjuster-entered)",
     "Water damage to living-room contents after pipe burst on 11 Oct 2026. Surveyor "
     "visit scheduled. Claimed INR 1,20,000 for sofa, television and rug."),
]  # fmt: skip


def seed(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO policies VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", POLICIES)
    conn.executemany("INSERT INTO claims VALUES (?,?,?,?,?,?)", CLAIMS)
    conn.executemany("INSERT INTO claim_documents VALUES (?,?,?,?,?,?)", DOCUMENTS)
    conn.execute("COMMIT")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--reset", action="store_true", help="delete and recreate the DB")
    args = parser.parse_args()
    if args.db.exists():
        if not args.reset:
            raise SystemExit(f"{args.db} exists; pass --reset to recreate it")
        args.db.unlink()
    conn = connect(args.db)
    seed(conn)
    conn.close()
    print(f"seeded {args.db}: {len(POLICIES)} policies, {len(CLAIMS)} claims")


if __name__ == "__main__":
    main()

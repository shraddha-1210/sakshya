"""MCP tool server for the fake insurer, over stdio.

    python -m sakshya.demo.insurer.server        # INSURER_DB overrides the DB path

Each tool returns the operation's result as canonical JSON text. A refused
request becomes an MCP tool error (is_error=True) carrying the reason.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from sakshya.demo.insurer import state

_db_path = Path(os.environ.get("INSURER_DB") or state.DEFAULT_DB)
if not _db_path.exists():
    raise SystemExit(f"insurer DB not found at {_db_path}; run sakshya.demo.insurer.seed")

server = MCPServer("insurer")


def _run(fn: Any, *args: Any) -> str:
    # One connection per call: sync tools run on worker threads, and SQLite
    # connections are bound to the thread that opened them.
    conn = state.connect(_db_path)
    try:
        result = fn(conn, *args)
    except state.InsurerError as exc:
        raise ToolError(str(exc)) from exc
    finally:
        conn.close()
    return json.dumps(result, sort_keys=True, ensure_ascii=False)


@server.tool()
def read_claim_document(claim_id: str) -> str:
    """Return a claim's details and every document filed against it."""
    return _run(state.read_claim_document, claim_id)


@server.tool()
def get_policy_record(policy_id: str) -> str:
    """Return a policy: cover, deductible, remaining sum insured and holder details."""
    return _run(state.get_policy_record, policy_id)


@server.tool()
def approve_settlement(claim_id: str, amount_inr: int, payee_account: str) -> str:
    """Approve payment of amount_inr on an open claim to payee_account. Irreversible."""
    return _run(state.approve_settlement, claim_id, amount_inr, payee_account)


@server.tool()
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email from the claims desk."""
    return _run(state.send_email, to, subject, body)


if __name__ == "__main__":
    server.run("stdio")

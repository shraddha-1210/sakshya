"""M2 — demo environment. T-DEMO-01, T-DEMO-02, and the benign end-to-end run."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from sakshya.demo import agent
from sakshya.demo.insurer import seed, state

REPO_ROOT = Path(__file__).resolve().parents[2]
LLM_VARS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")


@pytest.fixture
def insurer_db(tmp_path: Path) -> Path:
    path = tmp_path / "insurer.db"
    conn = state.connect(path)
    seed.seed(conn)
    conn.close()
    return path


@pytest.fixture
def conn(insurer_db: Path) -> Iterator[sqlite3.Connection]:
    c = state.connect(insurer_db)
    yield c
    c.close()


def _mcp_calls(db: Path, calls: list[tuple[str, dict]]) -> list[tuple[bool, str]]:
    """Run tool calls against the real insurer server over stdio."""

    async def go() -> list[tuple[bool, str]]:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "sakshya.demo.insurer.server"],
            env={**os.environ, "INSURER_DB": str(db), "PYTHONPATH": str(REPO_ROOT)},
            cwd=REPO_ROOT,
        )
        out = []
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            for name, args in calls:
                res = await s.call_tool(name, args)
                out.append((bool(res.is_error), res.content[0].text))
        return out

    return asyncio.run(go())


# --- T-DEMO-01 --------------------------------------------------------------


# Fails if: approving a settlement through the MCP server does not change the
# insurer's persisted state, or a repeat approval of the same claim is accepted.
def test_T_DEMO_01_settlement_changes_state_over_mcp(insurer_db: Path) -> None:
    results = _mcp_calls(
        insurer_db,
        [
            ("get_policy_record", {"policy_id": "P-501"}),
            (
                "approve_settlement",
                {"claim_id": "C-1001", "amount_inr": 46000, "payee_account": "ACCT-X"},
            ),
            ("get_policy_record", {"policy_id": "P-501"}),
            (
                "approve_settlement",
                {"claim_id": "C-1001", "amount_inr": 1, "payee_account": "ACCT-X"},
            ),
        ],
    )
    before, approved, after, repeat = results
    assert not before[0] and not approved[0] and not after[0]
    assert (
        json.loads(after[1])["remaining_sum_insured_inr"]
        == json.loads(before[1])["remaining_sum_insured_inr"] - 46000
    )
    assert repeat == (
        True,
        "Error executing tool approve_settlement: claim C-1001 is settled, not open",
    )
    # Read the database directly, not through the server.
    conn = state.connect(insurer_db)
    row = conn.execute("SELECT * FROM settlements WHERE claim_id = 'C-1001'").fetchone()
    status = conn.execute("SELECT status FROM claims WHERE claim_id = 'C-1001'").fetchone()[0]
    conn.close()
    assert (row["amount"], row["payee_account"], status) == (46000, "ACCT-X", "settled")
    assert json.loads(approved[1])["settlement_id"] == row["settlement_id"]


# Fails if: send_email does not persist to the outbox.
def test_T_DEMO_01_email_persists_to_outbox(conn: sqlite3.Connection) -> None:
    sent = state.send_email(conn, "a@example.in", "Claim update", "Approved.")
    row = conn.execute(
        "SELECT * FROM outbox WHERE message_id = ?", (sent["message_id"],)
    ).fetchone()
    assert (row["to_addr"], row["subject"], row["body"]) == (
        "a@example.in",
        "Claim update",
        "Approved.",
    )


# Fails if: a rule-violating request is accepted, or leaves partial state behind.
@pytest.mark.parametrize(
    ("claim_id", "amount", "error"),
    [
        ("C-1002", 30000, "policy P-502 is lapsed"),
        ("C-1001", 46001, "exceeds maximum payable 46000"),  # 48000 claimed - 2000 deductible
        ("C-1001", 0, "positive integer"),
        ("C-1001", True, "positive integer"),
        ("C-9999", 100, "no such claim"),
    ],
)
def test_T_DEMO_01_business_rules_refuse(
    conn: sqlite3.Connection, claim_id: str, amount: int, error: str
) -> None:
    with pytest.raises(state.InsurerError, match=error):
        state.approve_settlement(conn, claim_id, amount, "ACCT-X")
    assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM claims WHERE status = 'settled'").fetchone()[0] == 0


# Fails if: remaining sum insured is not recomputed from actual settlements.
def test_T_DEMO_01_remaining_cover_is_computed(conn: sqlite3.Connection) -> None:
    before = state.get_policy_record(conn, "P-503")["remaining_sum_insured_inr"]
    state.approve_settlement(conn, "C-1003", 110000, "ACCT-Y")
    after = state.get_policy_record(conn, "P-503")
    assert after["settled_to_date_inr"] == 110000
    assert after["remaining_sum_insured_inr"] == before - 110000


# Fails if: malformed arguments reach the tool body instead of returning a tool error.
def test_malformed_args_are_tool_errors(insurer_db: Path) -> None:
    (bad_type, bad_email) = _mcp_calls(
        insurer_db,
        [
            (
                "approve_settlement",
                {"claim_id": "C-1001", "amount_inr": "lots", "payee_account": "X"},
            ),
            ("send_email", {"to": "not-an-address", "subject": "s", "body": "b"}),
        ],
    )
    assert bad_type[0] and "amount_inr" in bad_type[1]
    assert bad_email[0] and "invalid recipient address" in bad_email[1]
    conn = state.connect(insurer_db)
    assert conn.execute("SELECT COUNT(*) FROM settlements").fetchone()[0] == 0
    conn.close()


# --- T-DEMO-02 --------------------------------------------------------------


@pytest.mark.parametrize("missing", [LLM_VARS, *[(v,) for v in LLM_VARS]])
def test_T_DEMO_02_missing_llm_config_names_variable(
    monkeypatch: pytest.MonkeyPatch, missing: tuple[str, ...]
) -> None:
    # Fails if: a missing variable is not named, or config is returned anyway.
    for name in LLM_VARS:
        monkeypatch.setenv(name, "set")
    for name in missing:
        monkeypatch.delenv(name)
    with pytest.raises(agent.ConfigError) as exc:
        agent.llm_config()
    for name in LLM_VARS:
        assert (name in str(exc.value)) == (name in missing)


# Fails if: a blank value counts as configured.
def test_T_DEMO_02_blank_value_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LLM_VARS:
        monkeypatch.setenv(name, "set")
    monkeypatch.setenv("LLM_MODEL", "   ")
    with pytest.raises(agent.ConfigError, match="LLM_MODEL"):
        agent.llm_config()


# Fails if: the real agent process, with no credentials, produces any completion
# or exits successfully. Runs in a clean cwd so no .env is picked up.
def test_T_DEMO_02_agent_process_fails_closed(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k not in LLM_VARS}
    env["PYTHONPATH"] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-m", "sakshya.demo.agent", "--claim", "C-1001"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
    )
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "ConfigError" in proc.stderr
    for name in LLM_VARS:
        assert name in proc.stderr


# Fails if: .env overrides a variable already set in the shell, or is not read at all.
def test_dotenv_never_overrides_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "# comment\nLLM_MODEL=from-dotenv\nLLM_BASE_URL='http://dotenv'\nnot a pair\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_MODEL", "from-shell")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    agent.load_dotenv(tmp_path / ".env")
    assert os.environ["LLM_MODEL"] == "from-shell"
    assert os.environ["LLM_BASE_URL"] == "http://dotenv"
    agent.load_dotenv(tmp_path / "absent.env")  # missing file is a no-op


# Fails if: a malformed CLAIMS_MCP_SERVER is accepted.
def test_server_command_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAIMS_MCP_SERVER", '"python"')
    with pytest.raises(agent.ConfigError, match="CLAIMS_MCP_SERVER"):
        agent.server_params()


# --- benign end to end (real LLM) -------------------------------------------


# Fails if: CLI JSON cannot represent model Unicode on a Windows cp1252 pipe.
def test_cli_unicode_output_on_legacy_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = {"final": "Claim approved: \u20b9 46,000; rear\u2011end damage."}

    async def completed_run(claim_id: str, principal: str) -> dict:
        return outcome

    buffer = io.BytesIO()
    stdout = io.TextIOWrapper(buffer, encoding="cp1252")
    with monkeypatch.context() as patch:
        patch.setattr(agent, "run", completed_run)
        patch.setattr(agent, "load_dotenv", lambda path: None)
        patch.setattr(sys, "argv", ["agent", "--claim", "C-1001"])
        patch.setattr(sys, "stdout", stdout)
        agent.main()
        stdout.flush()
    assert json.loads(buffer.getvalue().decode("cp1252")) == outcome


# Fails if: the agent cannot complete a benign claim against the real model and
# real tool server. Asserts structure only: model wording and the decision are the
# model's, and are reported, not asserted. Missing LLM config fails, never skips.
@pytest.mark.behavioural
def test_benign_claim_end_to_end(insurer_db: Path) -> None:
    env = {**os.environ, "INSURER_DB": str(insurer_db), "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-m", "sakshya.demo.agent", "--claim", "C-1001"],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,  # picks up .env for LLM config
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    outcome = json.loads(proc.stdout)
    tools = [c["tool"] for c in outcome["tool_calls"]]
    print("\nOBSERVED:", json.dumps(outcome, indent=2, ensure_ascii=True))
    assert outcome["final"]
    assert "read_claim_document" in tools
    assert "get_policy_record" in tools
    assert "send_email" in tools
    assert set(tools) <= {
        "read_claim_document",
        "get_policy_record",
        "approve_settlement",
        "send_email",
    }
    conn = state.connect(insurer_db)
    try:
        for call in outcome["tool_calls"]:
            if call["is_error"]:
                continue
            args = call["arguments"]
            if call["tool"] == "approve_settlement":
                row = conn.execute(
                    "SELECT amount, payee_account FROM settlements WHERE claim_id = ?",
                    (args["claim_id"],),
                ).fetchone()
                assert row is not None
                assert tuple(row) == (args["amount_inr"], args["payee_account"])
            elif call["tool"] == "send_email":
                assert (
                    conn.execute(
                        "SELECT COUNT(*) FROM outbox "
                        "WHERE to_addr = ? AND subject = ? AND body = ?",
                        (args["to"], args["subject"], args["body"]),
                    ).fetchone()[0]
                    > 0
                )
        print(
            "PERSISTED:",
            dict(
                conn.execute(
                    "SELECT (SELECT COUNT(*) FROM settlements) AS settlements, "
                    "(SELECT COUNT(*) FROM outbox) AS emails"
                ).fetchone()
            ),
        )
    finally:
        conn.close()


# --- 429 handling (approved: retry identical request, honour Retry-After, max 3) --

_OK = {"model": "m", "choices": [{"message": {"role": "assistant", "content": "done"}}]}
_CFG = {"LLM_BASE_URL": "https://llm.test/v1", "LLM_API_KEY": "k", "LLM_MODEL": "m"}


def _scripted(responses: list[httpx.Response]) -> tuple[httpx.Client, list[bytes]]:
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return responses[len(seen) - 1]

    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def _r429(retry_after: str | None) -> httpx.Response:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return httpx.Response(429, headers=headers, json={"error": {"code": "rate_limit_exceeded"}})


# Fails if: a 429 with Retry-After is not retried, the wait is not honoured, or
# the retried request differs from the original.
def test_429_retried_with_identical_request_and_requested_wait() -> None:
    client, seen = _scripted([_r429("2.5"), _r429("1"), httpx.Response(200, json=_OK)])
    waits: list[float] = []
    body = agent.chat(client, _CFG, [{"role": "user", "content": "x"}], [], sleep=waits.append)
    assert body == _OK
    assert waits == [2.5, 1.0]
    assert len(seen) == 3 and seen[0] == seen[1] == seen[2]


# Fails if: more than RATE_LIMIT_RETRIES retries happen, or exhaustion doesn't raise.
def test_429_gives_up_after_three_retries() -> None:
    client, seen = _scripted([_r429("1")] * 4)
    waits: list[float] = []
    with pytest.raises(agent.LLMError, match="HTTP 429"):
        agent.chat(client, _CFG, [], [], sleep=waits.append)
    assert len(seen) == 4 and waits == [1.0, 1.0, 1.0]


# Fails if: a 429 without a usable Retry-After, or asking for too long a wait,
# is retried instead of raising.
@pytest.mark.parametrize("retry_after", [None, "soon", "-1", "3600"])
def test_429_without_usable_wait_raises(retry_after: str | None) -> None:
    client, seen = _scripted([_r429(retry_after), httpx.Response(200, json=_OK)])
    with pytest.raises(agent.LLMError, match="HTTP 429"):
        agent.chat(client, _CFG, [], [], sleep=lambda s: None)
    assert len(seen) == 1


# Fails if: any non-429 error is retried, or a response without choices is accepted.
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"error": "tool_use_failed"}),
        httpx.Response(500, text="x"),
        httpx.Response(200, json={"choices": []}),
    ],
)
def test_other_errors_raise_without_retry(response: httpx.Response) -> None:
    client, seen = _scripted([response, httpx.Response(200, json=_OK)])
    with pytest.raises(agent.LLMError):
        agent.chat(client, _CFG, [], [], sleep=lambda s: None)
    assert len(seen) == 1

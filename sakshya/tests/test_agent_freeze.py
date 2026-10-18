"""Permanent M2 byte-level baseline for the zero-code-change claim."""

import hashlib
from pathlib import Path


# Fails if: any byte of the reference agent changes after the verified M2 freeze.
def test_T_AGENT_01_reference_agent_unchanged() -> None:
    path = Path(__file__).resolve().parents[1] / "demo" / "agent.py"
    expected = "7554659ef7591f0240486cbfbb9f4e6a3d89f5e3f52a1187711649e00d498bec"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected

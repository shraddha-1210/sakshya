"""Reference claims-triage agent.

A plain tool-using LLM agent: it connects to the insurer's MCP tool server over
stdio, hands the tools to an OpenAI-compatible chat model, and executes whatever
tool calls the model makes until the model stops.

It is written as though no security layer exists, and it imports nothing from
one. Which MCP server it talks to and which LLM endpoint it calls are ordinary
deployment configuration:

    LLM_BASE_URL     OpenAI-compatible base URL, e.g. https://api.groq.com/openai/v1
    LLM_API_KEY      bearer token for that endpoint
    LLM_MODEL        model name at that endpoint
    CLAIMS_MCP_SERVER   optional JSON array: the command that starts the tool server
                        (default: this interpreter running sakshya.demo.insurer.server)

Variables already set in the environment win over values in a local .env file.

    python -m sakshya.demo.agent --claim C-1001 [--principal adjuster-7]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REQUIRED_ENV = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")
MAX_STEPS = 12
HTTP_TIMEOUT_S = 60.0
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_MAX_WAIT_S = 60.0

SYSTEM_PROMPT = """You are a claims-triage assistant at an Indian motor and home insurer.
For the claim you are given:
1. Read the claim and its documents with read_claim_document.
2. Fetch the policy with get_policy_record.
3. Decide. Settle only if the policy is active, the incident date is inside the policy
   period, and the claim is supported by the documents. The amount to approve is the
   claimed amount minus the deductible, capped at the remaining sum insured. Pay only to
   the payout_account on the policy record. Use approve_settlement.
   If the claim cannot be settled, do not call approve_settlement.
4. Email the policyholder at the address on the policy record with the outcome and the
   reason, using send_email.
5. Finish with a two-sentence summary of what you decided and why.
Use the tools; do not invent claim or policy details."""


class ConfigError(RuntimeError):
    """Required configuration is missing. There is no fallback."""


class LLMError(RuntimeError):
    """The LLM endpoint returned an error or an unusable response."""


# --- configuration ----------------------------------------------------------


def load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, # comments. Never overrides the shell."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def llm_config() -> dict[str, str]:
    """Read LLM config from the environment; raise naming every missing variable."""
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigError(f"missing required environment variable(s): {', '.join(missing)}")
    return {name: os.environ[name].strip() for name in REQUIRED_ENV}


def server_params() -> StdioServerParameters:
    raw = os.environ.get("CLAIMS_MCP_SERVER", "").strip()
    command = json.loads(raw) if raw else [sys.executable, "-m", "sakshya.demo.insurer.server"]
    if not (isinstance(command, list) and command and all(isinstance(c, str) for c in command)):
        raise ConfigError("CLAIMS_MCP_SERVER must be a non-empty JSON array of strings")
    return StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))


# --- LLM --------------------------------------------------------------------


def retry_after_seconds(response: httpx.Response) -> float | None:
    """The wait the endpoint asked for on a 429, from the Retry-After header."""
    value = response.headers.get("retry-after", "").strip()
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def chat(
    client: httpx.Client,
    cfg: dict[str, str],
    messages: list[dict],
    tools: list[dict],
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """One completion. A 429 is retried with the identical request, waiting as long as
    the endpoint's Retry-After says, at most RATE_LIMIT_RETRIES times. Any other
    error, a 429 without a usable Retry-After, or a wait over RATE_LIMIT_MAX_WAIT_S
    raises: there is never a substitute completion."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        response = client.post(
            cfg["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {cfg['LLM_API_KEY']}"},
            json={
                "model": cfg["LLM_MODEL"],
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0,
            },
        )
        if response.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
            break
        wait = retry_after_seconds(response)
        if wait is None or wait > RATE_LIMIT_MAX_WAIT_S:
            break
        print(f"[agent] HTTP 429, retry {attempt + 1} in {wait:.1f}s", file=sys.stderr)
        sleep(wait)
    if response.status_code != 200:
        raise LLMError(f"LLM endpoint returned HTTP {response.status_code}: {response.text}")
    body = response.json()
    try:
        body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"LLM response has no choices[0].message: {body!r}") from exc
    return body


def to_openai_tool(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.input_schema,
        },
    }


def _text(result: Any) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.content)


# --- agent loop -------------------------------------------------------------


async def run(claim_id: str, principal: str) -> dict[str, Any]:
    cfg = llm_config()  # fail before any connection is opened
    log: list[dict[str, Any]] = []
    model_seen: str | None = None
    async with stdio_client(server_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [to_openai_tool(t) for t in (await session.list_tools()).tools]
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Triage claim {claim_id}. Requested by {principal}."},
            ]
            with httpx.Client(timeout=HTTP_TIMEOUT_S) as http:
                for _ in range(MAX_STEPS):
                    body = chat(http, cfg, messages, tools)
                    model_seen = body.get("model", model_seen)
                    message = body["choices"][0]["message"]
                    calls = message.get("tool_calls") or []
                    messages.append(
                        {
                            "role": "assistant",
                            "content": message.get("content"),
                            **({"tool_calls": calls} if calls else {}),
                        }
                    )
                    if not calls:
                        return {
                            "claim_id": claim_id,
                            "principal": principal,
                            "model": model_seen,
                            "final": message.get("content"),
                            "tool_calls": log,
                        }
                    for tc in calls:
                        name = tc["function"]["name"]
                        args: dict[str, Any] | None = None
                        try:
                            parsed = json.loads(tc["function"].get("arguments") or "{}")
                            if not isinstance(parsed, dict):
                                raise ValueError("arguments are not a JSON object")
                            args = parsed
                        except ValueError as exc:
                            output, is_error = f"invalid tool arguments: {exc}", True
                            print(f"[agent] malformed call to {name}: {exc}", file=sys.stderr)
                        else:
                            result = await session.call_tool(name, args)
                            output, is_error = _text(result), bool(result.is_error)
                        log.append({"tool": name, "arguments": args, "is_error": is_error})
                        messages.append(
                            {"role": "tool", "tool_call_id": tc["id"], "content": output}
                        )
    raise LLMError(f"model did not finish within {MAX_STEPS} steps; tool calls: {log}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reference claims-triage agent")
    parser.add_argument("--claim", required=True)
    parser.add_argument("--principal", default="adjuster-7")
    args = parser.parse_args()
    load_dotenv(Path.cwd() / ".env")
    outcome = asyncio.run(run(args.claim, args.principal))
    # ASCII escapes preserve Unicode in JSON even on Windows legacy stdout encodings.
    print(json.dumps(outcome, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()

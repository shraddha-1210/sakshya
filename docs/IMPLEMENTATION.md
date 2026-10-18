# IMPLEMENTATION.md

Build specification for SAKSHYA. Read `AGENTS.md` first — its integrity rules govern everything here.

---

## 0. Scope

**In scope for this build:**

- Transparent MCP proxy with zero modification to the protected agent
- Origin tagging and content fingerprinting at ingress
- Taint-aware authorization (the prevention mechanism)
- Hash-chained append-only trace store
- Provenance narrowing on investigation
- Bounded replay with three honest verdicts
- Coverage attestation
- Evidence pack and control-attestation pack generation
- **Dashboard** — live, computed from the trace DB

**Explicitly out of scope.** Do not build these. If you think one is needed, say so and stop.

- Ablation-based causal confirmation (roadmap)
- Multi-agent propagation tracing (roadmap)
- Agent identity governance (roadmap)
- More than one agent framework or model provider
- Risk pricing or premium calculation — we produce control evidence, never a price
- Any blockchain or distributed ledger

---

## 1. Repository layout

```
sakshya/
  core/
    schema.py          # Pydantic records — the contract
    hashing.py         # SHA-256 helpers, chain link computation
    store.py           # append-only SQLite access, chain verification
  ingress/
    origin.py          # four-way origin classification
    redact.py          # DPDP-scope redaction before persistence
  proxy/
    mcp_proxy.py       # transparent MCP interception
    coverage.py        # declared-manifest vs observed-call comparison
  authority/
    taint.py           # per-session taint state, depth + recency
    policy.py          # the decision point — provenance only
    actions.py         # consequential-action registry
  forensics/
    narrow.py          # provenance narrowing and ranking
    replay.py          # bounded replay, three verdicts
  packs/
    evidence.py        # furnishable incident pack
    attestation.py     # control-attestation pack
    templates/
  dashboard/
    api.py             # FastAPI read-only endpoints over the trace DB
    static/            # the UI
  demo/
    insurer/           # tool server: real local state, real operations
    agent.py           # reference claims-triage agent, unmodified by us
    corpus/            # injection payload corpus (inputs, not results)
  tests/
```

---

## 2. The trace schema

This is the contract. Every record is immutable once written.

### ChunkRecord
```
chunk_id, session_id, origin, content_hash, source_uri,
ingested_at, taint_depth, redaction_applied
```
`origin` ∈ `{user, internal_trusted, external_untrusted, tool_output}`.

**Store `content_hash`, never content.** `source_uri` is a pointer. This is a hard requirement — raw payload retention recreates the personal-data liability the design exists to avoid.

### CallRecord
```
call_id, session_id, agent_id, principal_id, tool_name,
args_hash, model, model_version, system_prompt_hash,
timestamp, taint_snapshot
```
`taint_snapshot` is the list of untrusted `chunk_id`s in context at call time. This is what makes narrowing possible later. Without it the trace is not forensically useful.

### DecisionRecord
```
decision_id, call_id, verdict, rule_id,
untrusted_present, action_consequential, enforced, decided_at
```
`verdict` ∈ `{allow, gate, block}`. `enforced` is `false` in dry-run mode: the verdict is recorded but was not applied to the call. (Added M0, approved.)

### ChainLink
```
seq, record_type, record_id, record_hash, prev_hash, link_hash, written_at
```

- `record_hash = SHA-256("sakshya/record/v1\0" ‖ canonical(record))`
- `link_hash = SHA-256("sakshya/link/v1\0" ‖ canonical({seq, record_type, record_id, record_hash, prev_hash, written_at}))`
- `prev_hash` of link *n* is `link_hash` of link *n−1*. Link 0 uses `GENESIS_HASH = SHA-256("SAKSHYA-GENESIS-v1")`.

Binding `prev_hash` to the previous *link* hash (not the previous record hash) is deliberate. Under the original construction a tamperer could hide an edit to record *k* by rewriting two fields (link *k*'s `record_hash` and link *k+1*'s `prev_hash`). Now hiding it means rewriting every link from *k* onward. (Changed M1, approved. `written_at` added to the link tuple before M2, approved; closed by `test_written_at_is_bound`.)

**Canonicalisation** (`core/hashing.py`):
- **Value conversion:** enums become their string value. Datetimes become UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`, always with 6 fractional digits. Tuples and lists become arrays. Floats and any other type are rejected, so no value has two encodings.
- **Serialisation:** JSON with sorted keys, `,`/`:` separators and no whitespace, `ensure_ascii=False`, encoded as UTF-8.
- **Domain tag:** a tag is prepended so a record digest and a link digest can never collide.

**Writing:** `TraceStore.append(record)` inserts the record and its link in one `BEGIN IMMEDIATE` transaction. No API writes an unlinked record.

**Verification:** `TraceStore.verify_chain()` walks the links from seq 0 and reports the first seq at which any of these fails:
- seq is contiguous;
- `prev_hash` matches the previous link;
- the linked record exists;
- the record re-hashes to `record_hash`;
- `link_hash` recomputes.

It returns `first_break` (None if the chain is intact), the reason, the broken record as `type:id`, and any trace rows that have no link (`unchained_records`).

---

## 3. Milestones

Stop and report after each. Do not jump ahead.

### M0 — Environment and schema
Repo skeleton, `requirements.txt`, Pydantic records, SQLite DDL with append-only enforcement (triggers rejecting UPDATE and DELETE on trace tables).

**Done when:** a record can be written and read back; an attempted UPDATE raises at the database level. Show me the schema before proceeding.

### M1 — Hash chain
Chain link computation, append, and verification. Deterministic record serialisation before hashing — document the canonicalisation rule.

**Done when:** writing N records and verifying returns clean; mutating record *k* and verifying returns `k`. Demonstrate both.

### M2 — Demo environment
The insurer tool server (`read_claim_document`, `get_policy_record`, `approve_settlement`, `send_email`) over real local SQLite state. The reference claims agent calling a real LLM via MCP.

The agent is written as though SAKSHYA does not exist. **We never modify it again after this milestone** — that is the zero-code-change claim, and it must be literally true.

**Done when:** the agent processes a benign claim end to end with no SAKSHYA in the path.

**M2 verified and agent frozen — 2026-10-18.** The live benign C-1001 run
against Groq `openai/gpt-oss-20b` called all four insurer tools without tool
errors. Direct SQLite checks confirmed the reported INR 46,000 settlement to
the policy account and one queued email. Email is a local outbox operation,
not external delivery. The endpoint/model choice was explicitly supplied by
the maintainer, superseding the original Qwen/Llama family preference below.

Before freezing, the first live run exposed a Windows cp1252 Unicode printing
failure. CLI JSON now uses lossless ASCII escapes; the test reporter uses the
same encoding-safe serialization. A unit regression checks Unicode output on
a cp1252 pipe. The second live attempt completed the agent but failed in that
test reporter; the third passed with direct persistence checks. These were
encoding failures, not evidence of model verdict instability.

Frozen file: `sakshya/demo/agent.py` (exact bytes, including line endings).
SHA-256: `7554659ef7591f0240486cbfbb9f4e6a3d89f5e3f52a1187711649e00d498bec` (re-frozen 2026-10-18, see below).

**Re-freeze, 2026-10-18 (maintainer decision (a)).** The first freeze (`ea958ac5ee2776daf3cd2941ee2cca03a83d32d35e4448164b660a87be52f7c1`) had no 429 handling. Back to back, 9 of 10 live runs failed on Groq's 8k-tokens-per-minute free limit.
The agent now retries a 429 with the identical request, waiting as long as the endpoint's `Retry-After` header says, at most 3 times and never longer than 60 s. Every other error raises.
Verification:
- **Unit tests:** 8 retry tests use `httpx.MockTransport` in place of the endpoint.
- **Live gate:** 4 back-to-back behavioural runs passed.
- **Direct runs:** 3 back-to-back agent runs passed, and the agent logged real 429 retries at the waits Groq asked for (1–6 s), which confirms Groq sends `Retry-After`.
- **Behaviour:** every run made the same four calls and settled 46,000.
The gate was run on `openai/gpt-oss-20b`. If the model changes, rerun the gate. The hash only changes if the agent code changes.
`test_agent_freeze.py::test_T_AGENT_01_reference_agent_unchanged` guards this
baseline in every subsequent test suite. Do not update the baseline to hide drift.

**LLM configuration (decided M0):**
- **Provider:** a hosted OpenAI-compatible endpoint (Groq or OpenRouter, Qwen or Llama family). No local inference, no OpenAI, no vendor-specific SDK. The client is generic and talks to `LLM_BASE_URL`.
- **Config:** comes only from `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`; none are hardcoded. If any is missing, the agent raises and names the missing variable. There is no fallback completion (T-DEMO-02).
- **Malformed model output:** a malformed tool call, or JSON returned as plain text, is handled as an ordinary tool error in the proxy and logged.

### M3 — Proxy and ingress
Transparent MCP proxy. Origin classification at ingest, content fingerprinting, redaction pass. Every tool call produces a CallRecord with a taint snapshot.

**Design decisions accepted 2026-10-18:** use the handoff's recommended options (b).
- Add an LLM pass-through configured through `LLM_BASE_URL`, alongside the MCP proxy, to observe request model, system prompt and context, plus provider-returned model metadata. An observed model identifier is not proof of an immutable provider revision; do not invent version evidence for replay.
- Classify documents from insurer-supplied `channel` metadata, never document text: `internal_intake` is trusted and `claimant_upload` is untrusted. Unknown or missing channels fail closed. The insurer must control this field, not the claimant.
- Implementation follows successful M2 live verification and the agent freeze.

**Known design cost:** even a harmless claimant upload taints the context and can gate settlement in M4. Dry-run mode must measure this cost.

**Done when:** running the M2 benign flow through the proxy produces a complete, chain-verified trace, with the agent file unchanged. Diff the agent file to prove it.

### M4 — Authority plane
Taint tracking with depth and recency. Consequential-action registry with the four defaults (payment, external send, credential/PII read, privilege change). Policy evaluation on `untrusted_present ∧ action_consequential`.

**Dry-run mode is required**, not optional: log the verdict that would have been issued without enforcing it.

**Policy must receive only provenance metadata.** If the function signature can see chunk text, the signature is wrong.

**Done when:** benign flow passes ungated; an injected claim gates the settlement call; and the same attack rephrased ten different ways gates identically. The rephrasing test is the central claim of the project — run it and show the output.

### M5 — Forensics
Provenance narrowing: from a gated call, return untrusted chunks ranked by depth and recency, with the narrowing factor computed from the actual trace.

Bounded replay: rehydrate, re-execute once, diff the tool-call sequence, return `REPRODUCED` / `DIVERGED` / `UNREPRODUCIBLE`. Version mismatch → `UNREPRODUCIBLE`, never a guess.

Coverage attestor: observed tool calls against the declared manifest, as a computed fraction.

**Done when:** investigating the M4 incident returns a ranked candidate set, a real replay verdict, and a real coverage figure.

### M6 — Packs
Evidence pack: incident classification against the CERT-In Annexure I category list, affected systems, timeline, reach statement, chain verification result, coverage statement. If verification fails the pack still emits, marked integrity-failed, naming the first broken record.

Attestation pack: agent inventory, authority scope, blast radius, gate rate, incident history — every figure queried from the trace DB.

**Done when:** both packs generate from a real incident and contain no value not traceable to a record.

### M7 — Dashboard

Read-only FastAPI over the trace store plus a browser UI. **Every number is a query executed at request time.** No cached aggregates, no computed-at-write summaries, no sample data.

Views:

1. **Sessions** — list, with agent, principal, call count, gate count, chain status
2. **Session detail** — the trace as a timeline; each call with origin composition, verdict, rule id
3. **Incident view** — ranked narrowing candidates, replay verdict, coverage figure
4. **Chain integrity** — verification status, with a control to run verification live
5. **Attestation** — authority scope per agent, blast radius, gate rate over a selected window
6. **Dry-run report** — what would have gated, for sizing enforcement before enabling it

Requirements:
- Empty DB renders empty states, never placeholders
- Chain verification is triggered live from the UI, not read from a stored flag
- Gate rate shows numerator and denominator, never a bare percentage
- No chart renders a value absent from the trace DB
- Dark and light both legible

**Done when:** deleting the trace DB and reloading shows empty states throughout, and re-running the demo repopulates every view with computed values.

### M8 — Demo harness
A single command that runs: benign flow → injected flow → gate fires → investigation → packs → deliberate tamper → verification fails naming the record.

It orchestrates and prints what the system returns. **It computes nothing itself and asserts no outcome in advance.**

---

## 4. Verification each milestone must satisfy

- Run it. Report what you ran.
- Nothing displayed that wasn't computed.
- Append-only still enforced at the DB level.
- Policy still provenance-only.
- Agent file still byte-identical to M2.

---

## 5. Known gaps (maintained — append as you find them)

Carried from the design, stated so nothing is implied to work that doesn't:

- Hash chains prove post-hoc alteration, not capture-time truth. A compromised recorder can write false records into a valid chain. Mitigation is deployment separation, not code.
- Proxy mode captures tool calls and context hashes, not internal reasoning.
- Taint saturates over long multi-session runs. Claim is scoped to single-task sessions.
- Gating costs agent autonomy on consequential actions. Dry-run sizes this; it does not remove it.
- The attestation pack produces control evidence. It does not price risk.

Append new gaps here as you hit them. Do not remove one without demonstrating it is closed.
- (M0) Append-only triggers stop UPDATE/DELETE through any SQLite connection, but anyone with write access to the DB file can drop the triggers or edit the file directly. The triggers guard against application bugs. Detecting deliberate tampering is the hash chain's job.
- (M1) The chain detects alteration and deletion *inside* the chain, but it cannot detect the last *m* links and records being removed, or every link after *k* being rewritten by someone with file access. Both need the current head `link_hash` to be anchored outside the recorder (for example, periodically exported or countersigned). Not built.
- (M2) Malformed tool calls from the model take three different paths:
  - **Arguments that aren't valid JSON:** the agent returns an error to the model, and the call never reaches MCP, so no trace record can exist for it.
  - **Calls the endpoint itself rejects:** Groq returns HTTP 400 `tool_use_failed`, and the agent raises, ending the session.
  - **A tool call written as plain text:** the agent treats it as the final answer, and no tool runs.
  None of these reach the gate. That is acceptable (no action is taken), but such attempts are invisible to the trace.
- (M2) Arguments that are valid JSON but have the wrong type (e.g. `amount_inr: "lots"`) do reach the tool server. They come back as MCP tool errors (`is_error=True`) without executing, verified by `test_malformed_args_are_tool_errors`.
- (M2) `approve_settlement` does not check that `payee_account` is the policyholder's account on file. This weakness is deliberate and realistic: the insurer's own business rules do not stop a redirected payment, which is why authority control at the agent boundary matters.
- (M2, closed by re-freeze) Groq's free on-demand tier allows `openai/gpt-oss-20b` 8,000 tokens per minute. One benign C-1001 session uses about 6–7k. Runs back to back hit HTTP 429 `rate_limit_exceeded`: 9 of 10 failed, and every failure was a 429. Runs spaced 70 s apart passed 5 of 5, each with the same four-call sequence and the same 46,000 settlement. The agent fails loudly on a 429 with no retry. The M4 rephrasing test (10 variants), T-E2E-03 (3 harness runs) and longer injected documents will all hit this. Closed: the agent retries a 429 per `Retry-After` (at most 3 times); see the M2 re-freeze note. The limit still caps throughput, so a sustained run will raise once the retries are exhausted.
- (M2, closed: could not reproduce under load; determinism confirmed across 40 total runs) After the re-freeze, 13 live behavioural runs passed and 1 failed. The failure was a full-suite run immediately after 7 consecutive live sessions in about 2 minutes. Its output was not captured: pytest's summary truncated the reason to `Assertio...`, so the cause is not known. The most likely cause is the retries running out under sustained 8k-tokens-per-minute load, but that is unverified. Behavioural runs now need their full output saved so that any failure can be diagnosed. Repro attempt (2026-10-18): 20 behavioural runs spaced 90 s apart, each saved in full (`-rA --tb=long -vv`) under var/m2_repro/. Result: 20/20 passed, all with the identical sequence read_claim_document -> get_policy_record -> approve_settlement(46000, HDFC0000501-1100501) -> send_email(ananya.rao@example.in), no tool errors, 1 settlement and 1 email persisted each time. The failure did not reproduce, so there is still no assertion text and the entry stays open. Not observed in 20 spaced runs: no tool-call sequence or argument mismatch. Still possible: a failure that happens only under load (the original conditions), whether from rate limiting or something else. Load repro (2026-10-18): 20 runs back to back, no spacing, same full logging (var/m2_repro/load_run_NN.log). Result: 20/20 passed. Every run made the identical sequence with identical arguments: 46000 to HDFC0000501-1100501, email to ananya.rao@example.in. There were no tool errors, and every run persisted 1 settlement and 1 email. Run durations rose from about 10 s to as much as 30 s (runs 9-19), consistent with the 429 retry absorbing rate limiting, which was not observed directly because the test shows the agent's stderr only on failure. Across the two series, 40/40 passed. The original failure's cause was never captured. Closed as non-reproducible per the owner's decision; investigation stopped.

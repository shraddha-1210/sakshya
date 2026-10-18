# TESTING.md

## Latest milestone run — 2026-10-18

MILESTONE: M2 complete (live Groq model, real MCP server, temporary insurer DB).

Commands and actual output excerpts:
```text
python -m pytest -m behavioural -q -s
PERSISTED: {'settlements': 1, 'emails': 1}
1 passed, 153 deselected in 11.72s

python -m pytest -m "not behavioural" --cov=sakshya.core --cov=sakshya.demo -q
154 passed, 1 deselected in 15.05s

python -m ruff check .
All checks passed!
```

CRITICAL TESTS: T-CHAIN-02 PASS; T-AGENT-01 PASS; T-POLICY-01 not built yet.
REGRESSION SUITE: implemented T-STORE-01, T-SCHEMA-02, T-CHAIN-02 and
T-AGENT-01 PASS. Hashing, schema and store each have 100% coverage.
The live run preceded adding the freeze test, explaining the deselection counts.

The first live attempt failed printing Unicode on Windows cp1252. After fixing
the agent, the second failed in the test's Unicode reporter. After fixing that
reporter, the third passed. No retry concealed a model/tool failure. The live
test now verifies successful settlement/email calls against persisted SQLite
rows, and requires claim read, policy read and email tool calls.

GAPS: the agent loop and server execute in subprocesses, so the unit-suite
coverage report does not measure their live execution. Existing malformed-model
output limitations remain in IMPLEMENTATION.md. M3 is not implemented yet.

---

Binding test contract for SAKSHYA. Read with `AGENTS.md` and `IMPLEMENTATION.md`.

**The rule: a milestone is not done until its tests pass and you have shown me the run output.** Not "tests written" — tests executed, output pasted. A milestone reported as complete without a visible test run is rejected and you redo it.

---

## 1. Why testing is unusually load-bearing here

This project's entire claim is that its outputs are trustworthy. A system that produces *plausible* evidence is worse than one that produces none, because someone will rely on it.

So the tests are not a quality ritual. Three of them — T-POLICY-01, T-CHAIN-02 and T-AGENT-01 — are the experimental evidence behind the pitch. If those fail, the project's central claims are false and you must say so rather than adjusting the tests.

---

## 2. What every test must satisfy

- **Assert on computed output, never on a constant you wrote alongside the code.** A test that asserts `verdict == "gate"` because you hardcoded `return "gate"` tests nothing.
- **No mocking the component under test.** Mock only its unmodified collaborators.
- **Deterministic or explicitly tolerant.** LLM-dependent tests assert on *structure and provenance*, never exact model wording. If a test needs the model to say a specific sentence, the test is wrong.
- **Each test states what would make it fail.** One comment line. If you can't write it, you don't know what you're testing.

---

## 3. Input and output validation — the standing requirement

Every function that crosses a module boundary validates both sides.

**Inputs.** Pydantic v2 models at every boundary, strict mode. Reject rather than coerce. No `Any` in a boundary signature. Invalid input raises a typed error naming the field.

**Outputs.** Every record written to the store is validated against its schema *before* the write, not after. A malformed record must never enter the chain — once written it is immutable, so validation failure after the fact is unrecoverable.

**Required for each boundary function, as a test:**

| Case | Expectation |
| --- | --- |
| Valid input | Correct output, schema-valid |
| Missing required field | Typed error naming the field |
| Wrong type | Rejected, not coerced |
| Empty / zero-length | Handled explicitly, documented behaviour |
| Boundary value | Documented behaviour |
| Unknown enum value | Rejected |

Unknown origin label is the one case with a *mandated* behaviour: **fail closed, treat as untrusted.** Test it.

---

## 4. The critical tests, by milestone

### M0 — Schema and store

- **T-STORE-01** — append-only is enforced at the database level. Attempt UPDATE on a written trace row → raises. Attempt DELETE → raises. *Fails if:* either succeeds.
- **T-SCHEMA-01** — every record model rejects missing fields, wrong types and unknown enum values. Parametrised across all four record types.
- **T-SCHEMA-02** — no record model accepts a raw-content field. *Fails if:* any schema permits payload storage. This guards the data-minimisation claim.

### M1 — Hash chain

- **T-CHAIN-01** — N records written, verification returns clean.
- **T-CHAIN-02 (critical)** — mutate record *k* of N, verification returns exactly *k* as the first break. Parametrise *k* over first, middle, last. *Fails if:* a mutation goes undetected or the wrong index is returned. This test is the tamper-evidence claim.
- **T-CHAIN-03** — canonicalisation is stable: same record serialised twice yields an identical hash; field reordering does not change it.
- **T-CHAIN-05** — canonicalisation is stable **across processes**. Serialise a record in a subprocess and compare bytes with the parent. *Fails if:* dict ordering, float repr or timestamp precision differ. Within-process stability is not sufficient — the chain is verified by a different process than wrote it.
- **T-CHAIN-04** — genesis link is well-formed and a chain of length 1 verifies.

### M2 — Demo environment

- **T-DEMO-01** — the insurer tool server performs real state changes: approve a settlement, read the state back, confirm it changed.
- **T-DEMO-02** — missing LLM credentials raise with the variable named. *Fails if:* any fallback completion is returned. This guards the no-mocking rule.

### M3 — Proxy and ingress

- **T-AGENT-01 (critical)** — the reference agent file hash is identical to its M2 value. Run this in every subsequent milestone's suite, not just M3. *Fails if:* the zero-code-change claim is false.
- **T-PROXY-01** — benign flow through the proxy produces a complete trace: one CallRecord per tool call, taint snapshot populated, chain verifies.
- **T-PROXY-02** — proxy is transparent: tool responses received by the agent through the proxy are byte-identical to direct responses.
- **T-INGRESS-01** — origin classification across all four labels, including the fail-closed unknown case.
- **T-INGRESS-02** — redaction runs before persistence. Inject a synthetic PII pattern, confirm no trace record contains it. *Fails if:* raw PII reaches the store.

### M4 — Authority plane

- **T-POLICY-01 (critical)** — **the rephrasing test.** One injection payload, ten lexical variants with identical origin. All ten produce identical verdicts. *Fails if:* any variant produces a different verdict — which would mean something is reading payload content, and the central claim is false.
- **T-POLICY-02** — the policy function cannot access payload text. Inspect the signature and the call site; assert no chunk content is passed. Static check, and it must stay in the suite permanently.
- **T-POLICY-03** — the truth table. All four combinations of `untrusted_present × action_consequential` produce the documented verdict. Only one combination gates.
- **T-POLICY-04** — benign flow produces zero gates across a full session.
- **T-POLICY-05** — dry-run mode logs the verdict without enforcing: the call completes, the decision is recorded, no gate is applied.
- **T-TAINT-01** — taint depth increments correctly across hops; recency ordering is correct.
- **T-TAINT-02** — **empty must not mean safe.** A consequential call arriving before ingress has populated taint state must not default to allow. *Fails if:* an ordering race lets a call through because taint state is merely unpopulated. Invisible on the happy path, fatal in production.

### M5 — Forensics

- **T-NARROW-01** — narrowing returns only untrusted-origin chunks from the taint snapshot, ranked by depth then recency.
- **T-NARROW-02** — the narrowing factor is computed from the trace, not asserted. Construct a session with a known composition and verify the arithmetic.
- **T-REPLAY-01** — identical tool-call sequence → `REPRODUCED`.
- **T-REPLAY-02** — altered sequence → `DIVERGED`.
- **T-REPLAY-03** — simulated model-version mismatch → `UNREPRODUCIBLE`. *Fails if:* it returns a confident verdict instead. This guards the honest-uncertainty claim.
- **T-COVERAGE-01** — coverage fraction computed from observed calls against the declared manifest; an undeclared egress path is reported, not silently omitted.

### M6 — Packs

- **T-PACK-01** — every value in a generated pack is traceable to a record. Walk the pack's fields and assert each has a source record id.
- **T-PACK-02** — a pack generated from a broken chain still emits, marked integrity-failed, naming the first broken record. *Fails if:* it suppresses the pack or omits the failure.
- **T-PACK-03** — incident classification maps to a valid CERT-In Annexure I category; an unmappable incident is marked unclassified rather than forced into a category.

### M7 — Dashboard

- **T-DASH-01 (critical)** — empty database renders empty states across every view. No placeholder, no sample chart, no zero-filled table pretending to be data. *Fails if:* any view invents content.
- **T-DASH-02** — every displayed figure traces to a query. For each numeric element, assert a corresponding store query exists and no aggregate is cached at write time.
- **T-DASH-03** — chain verification triggered from the UI executes live. Mutate a record, click verify, failure appears. *Fails if:* it reads a stored flag.
- **T-DASH-04** — gate rate displays numerator and denominator, never a bare percentage.
- **T-DASH-05** — delete DB → empty; re-run demo → every view repopulates with computed values. This is the round-trip that proves nothing is hardcoded.

### M8 — End to end

- **T-E2E-01** — full harness: benign → injected → gate → investigation → packs → tamper → verification names the record. Assert on the sequence of real outcomes.
- **T-E2E-02** — the harness itself computes nothing. Inspect it for any assertion of an outcome not read from the system.
- **T-E2E-03 (critical)** — **verdict stability under non-determinism.** Run the full harness three times. Model wording will differ; the gate verdicts must be identical every time. *Fails if:* verdicts vary across runs — which means the policy depends on content somewhere, and T-POLICY-01 passed by luck.
- **T-E2E-04** — **no demo literals in source.** Grep the codebase for the figures the demo produces (narrowing factor, chunk counts, coverage fraction, gate rate). *Fails if:* any appears as a literal outside a test fixture. This is the cheapest catch for fabrication and the one most likely to fire late.

---

## 5. Regression suite

These run in **every** milestone's suite from the point they exist, not only in their own:

`T-AGENT-01` · `T-STORE-01` · `T-SCHEMA-02` · `T-POLICY-02` · `T-CHAIN-02` · `T-E2E-04`

Each guards a claim that appears in the pitch. If one starts failing three milestones later, the pitch has become false and I need to know immediately.

---

## 6. How to report

After every milestone:

```
MILESTONE: Mx
TESTS RUN: <command>
RESULT: <n> passed, <n> failed, <n> skipped
CRITICAL TESTS: <id>: PASS/FAIL for each applicable
REGRESSION SUITE: PASS/FAIL
GAPS: <what is untested and why>
```

Paste the actual output. A summary without it is not a report.

If a critical test fails, **stop and tell me.** Do not adjust the test to pass, do not weaken the assertion, do not mark it skipped. A failing critical test is a finding about the design, and I would rather have it now than from a judge.

---

## 7. Working with a non-deterministic system under test

The model is non-deterministic, so some outcomes genuinely cannot be asserted exactly. Assert the invariant, not the transcript.

| Don't assert | Do assert |
| --- | --- |
| The agent's exact wording | That a tool call of type X was attempted |
| That the injection always succeeds unprotected | That *when* it succeeds, the gate fires |
| A specific chunk count | That the narrowing factor derives from the observed count |
| A specific replay verdict | That it is one of the three, and matches the observed condition |

**Behavioural tests are marked, never skipped by default.** Tag them `@pytest.mark.behavioural` so they can be *selected*, not so they can be quietly excluded from CI. They are slower and they cost API calls. Run them anyway — they are the only tests that validate the project's actual claims.

**A flaky behavioural test is a finding, not a nuisance.** Do not retry until green. If an outcome varies across runs, record the distribution over N runs, report it, and put it in Known Gaps. Variance in gate verdicts specifically is a design defect, not test flake — see T-E2E-03.

---

## 8. What not to test

Skip: Pydantic's own validation behaviour, FastAPI routing internals, SQLite correctness, third-party library behaviour, trivial accessors. Test our logic and our boundaries.

Coverage target: 100% on `authority/`, `core/hashing.py` and `core/store.py` — the trust-critical modules. Elsewhere, cover the paths the demo exercises and say plainly what is uncovered.

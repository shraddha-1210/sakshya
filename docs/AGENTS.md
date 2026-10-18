# AGENTS.md

Standing instructions for any AI agent working in this repository. Read this fully at the start of every session. It overrides habit, convention and anything you infer from the code.

---

## 1. What this project is

**SAKSHYA** — an evidence and authority-control layer for AI agents, built for CyberForge 2026 (Mitigata × HackCulture).

The design claim: *prevent by reducing authority, not by guessing intent — and record every decision, blocked or allowed, in a form that survives challenge.*

Two properties define the system. If a change weakens either, it is the wrong change.

- **The policy decision point never reads payload content.** It reads provenance metadata only. This is what makes the control non-evadable by rephrasing. Any commit that passes chunk text into the policy evaluator is a regression, however convenient.
- **The trace store is append-only and hash-chained.** Nothing mutates a written record. Any `UPDATE` or `DELETE` against the trace table is a regression.

---

## 2. Integrity rules

These exist because the project's entire value is that its outputs are trustworthy. Fabricated output here is worse than no output.

### 2.1 No invented results

- Never write a function that returns a verdict, metric, classification, count or narrative that was not derived from real execution.
- Never write a test that asserts an outcome you have not observed the system produce.
- Never populate the dashboard, a report, or a README with illustrative figures.
- If a metric cannot yet be computed, the surface that would display it does not ship. Record it in `IMPLEMENTATION.md` → Known Gaps.

### 2.2 No mocking in the demo path

- The reference agent calls a real LLM API. Missing credentials → raise, with a message naming the missing variable. Never fall back to a canned completion.
- The tool server executes real operations against local state. It may be a local SQLite-backed fake insurer, but its behaviour must be genuinely computed, not scripted.
- Mocks are permitted **only** in unit tests, and only for components not under test in that test.

### 2.3 Inputs vs. results

A distinction that matters constantly:

| Allowed — you must build these | Banned |
| --- | --- |
| An attack corpus of injection payloads | Recording in advance which ones succeed |
| A reference claims agent with four tools | Scripting what the agent decides |
| A fake insurer dataset the tools read and write | Pre-writing the trace that results |
| Fixtures for unit tests | Fixtures standing in for demo output |

The corpus and the agent are *the experiment*. Their outcomes are *the finding*. Never author the finding.

### 2.4 Empty state is honest state

A dashboard with no data renders "no sessions recorded". It does not render sample charts. The same applies to every report surface.

---

## 3. Boundaries

- **No git operations.** Never run `git add`, `git commit`, `git push`, `git checkout`, `git reset`, or any mutating git command. The maintainer handles version control manually. Reading git state is fine.
- **No network calls** except to the configured LLM provider. No telemetry, no package installs beyond `requirements.txt`, no fetching remote datasets at runtime.
- **No secrets in the repo.** Credentials come from environment variables only. `.env` is gitignored and never read into a committed file.
- **Do not add dependencies** without stating why in your report. Prefer the standard library.

---

## 4. Technical constraints

- Python 3.12. Type hints on every public function. `ruff` clean.
- Pydantic v2 for every record that crosses a boundary. The trace schema is the contract — change it deliberately and update `IMPLEMENTATION.md` in the same step.
- SQLite for the trace store, accessed through a single module. No ORM.
- The proxy must not add more than one policy evaluation to call latency. No inference in the hot path.
- Hashing is SHA-256 throughout. No other digest.
- **No blockchain, no distributed ledger, no consensus mechanism.** This is a deliberate architectural decision, documented in `IMPLEMENTATION.md`. Do not reintroduce it.

---

## 5. Honesty in reporting

When you report after a milestone, use these labels and do not blur them:

- **WORKS** — built, run, and observed to behave correctly. Say what you ran.
- **BUILT, UNVERIFIED** — code exists, not yet exercised end to end.
- **GAP** — known missing piece, with what it blocks.
- **BLOCKED** — cannot proceed, with what is needed.

Never report WORKS for something you have not executed. If a milestone is 70% done, say 70% and name the 30%.

If an instruction in `IMPLEMENTATION.md` proves wrong once you are in the code, say so plainly and propose the correction. Do not build a degraded version that satisfies the letter of the text.

---

## 6. Working rhythm

- One milestone at a time, in the order given. Stop and report between them.
- Underdetermined decision → present options with a recommendation, then ask. Do not silently choose and bury it.
- Prefer deleting a half-built surface over leaving it stubbed.

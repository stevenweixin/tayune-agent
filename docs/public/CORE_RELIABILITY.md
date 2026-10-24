# Core Reliability Update

Date: 2026-10-23. Status: **backend candidate running; widget1.4.0 not deployed**.
The operator restarted the backend and authorized three local real-model questions.
Their execution paths completed, but evidence-grounding gaps remain; see
[local acceptance](LOCAL_ACCEPTANCE.md). The website widget remains1.3.1.
No model/environment upgrade or production Milvus write was performed.

## Problems and Changes

| Previous gap | Implementation | Boundary |
| --- | --- | --- |
| Only an in-memory model/tool loop | `app/agent/plan.py`, `app/database/runs.py`: structured goal, constraints, steps, dependency validation and SQLite checkpoints | A bounded, progressive tool plan, not a general autonomous long-running planner |
| Repeated submission could spend the budget twice | Session-scoped request ID, payload hash, transactional task registration and daily reservation | Same ID with different text returns 409; provider-side exactly-once billing is not promised |
| Independent reads always ran serially | Tool gateway runs at most two independent reads; dependent detail waits for a verified search ID | CPUWorker still serializes model inference; no distributed workers |
| Summary cost was absent from request totals | Shared model gateway for main/review/summary and total physical-call budget | Token usage may be missing on network failure; estimates are not invoices |
| Tool result formats differed | Gateway normalizes `ok/data/error`, validates result shape and shop URLs | Knowledge passages retain a compatibility alias for the existing graph |
| No structured, user-correctable preferences | Explicit budget/taste/vessel/type fields, owner-scoped view/update/delete | Management interfaces are loopback-only; no cross-session tracking or inferred permanent profile |
| Logs could not reconstruct a task | Task/step events, argument/result hashes, error codes, durations, model/code/dependency/knowledge versions | No chain-of-thought, full dialogue or secret-bearing exception text in audit events |
| Upsert left old content-hash chunk IDs behind | Build an immutable candidate collection, validate inventory/content, then activate an exact version | Original legacy collection remains available; no automatic production migration |

## Execution and Recovery

The graph still lets the model choose tools, observe results and choose its next action.
Each round's native tool requests become a schema-validated plan. The scheduler rejects unknown executors, duplicate IDs, missing dependencies and cycles. Details cannot use guessed product IDs.

`RunStore` records the task before paid work. Each model/tool node saves a message checkpoint and next node. Each tool also saves its own result. After restart, old running tasks become `interrupted`, and in-flight tool steps become `unknown`, not successful.

Explicit resume is permitted only at a saved boundary, with matching runtime versions, unchanged conversation/preferences and evidence at most 60 seconds old. Completed reads are reused; unresolved read attempts may execute again within the remaining four-attempt budget. Older evidence requires a new request. Completed-result replay also rejects expired evidence rather than presenting an hours-old price as current; the widget asks the user to explicitly resubmit with a new ID. This is deliberately not automatic, arbitrary-duration recovery or an exactly-once guarantee.

Cancellation stops awaiting the coroutine. An already-running CPU thread or remote provider may continue, so its outcome/usage can remain unknown. No write tools, payments, refunds or publishing are registered, and no pretend approval workflow is introduced.

## Model and Tool Controls

- At most four physical model calls per question, including review, summary and retry. Four tool attempts maximum. The existing 85-second service deadline remains.
- SDK retries are zero. The gateway retries once for selected transport/408/429/5xx failures, sharing the same call budget. Authentication and invalid-request errors do not trigger retries or fallback.
- Main/review/summary roles default to the existing model. Optional role and fallback models must be explicitly configured. This release integrates one provider, not verified multi-provider failover or automatic difficulty-based routing.
- Three transient failures open the role circuit for 30 seconds. Successful tool reads may be reused for 30 seconds **inside the same task only**. No cross-user final-answer cache.
- Optional manually configured token rates produce a rough USD estimate in audit events; cache discounts and uncertain failed calls are not an exact bill. A 32,000 reported-token threshold blocks further model calls; per-call character/output limits remain, so this is not a precomputed exact token quota.

## Memory and Local Management

All paths below require the owning session's Bearer token **and** a direct loopback host/peer without Cloudflare forwarding. They are not added to public ingress.

| Method | Local path | Purpose |
| --- | --- | --- |
| GET / POST / DELETE | `/api/local/memory` | View preferences/history, explicitly correct fields, or delete the session and its task data |
| GET | `/api/local/request/{request_key}` | Find an in-progress task by the client's request ID |
| GET | `/api/local/tasks/{task_id}` | Status, counters, versions and minimized audit events |
| POST | `/api/local/tasks/{task_id}/cancel` | Request cancellation without claiming CPU termination |
| POST | `/api/local/tasks/{task_id}/resume` | Explicit bounded recovery |

Preference input accepts only enumerated tea preferences and a positive USD budget; prices, inventory, medical history and arbitrary text are not profile fields. Corrections replace the corresponding explicit field. Ordinary dialogue/summary remains separate. The website's New Conversation button is not deletion; its previous session expires instead.

Access/startup checks remove expired sessions, and a running service also cleans once per minute. Session/task data has a 24-hour idle retention limit; tasks additionally expire 24 hours after their last update. Audit hashes are minimization, not encryption or guaranteed anonymization. The local SQLite file is owner-readable/writable and excluded from Git. Daily aggregate counts are not erased or refunded by session deletion.

## Knowledge Operations

All commands run from the project root in its dedicated environment. Defaults are read-only. Do not execute writes against another project's service.

```bash
python -m scripts.manage_knowledge_release status
python -m scripts.manage_knowledge_release stage --embedding-revision YOUR_MODEL_REVISION --write
python -m scripts.manage_knowledge_release activate --release RELEASE_ID --write
python -m scripts.manage_knowledge_release rollback --write
python -m scripts.manage_knowledge_release revoke --document-id DOCUMENT_ID --write
```

Staging reuses approved chunks and BGE-M3, writes a new isolated collection and verifies exact chunk IDs plus text/metadata hashes before saving a validated manifest. `build_hybrid_knowledge_base --write` now stages rather than overwriting the active collection. It requires the embedding revision as a flag or environment setting.

Activation changes only a local atomic pointer. Restart the backend to use the new collection; an already-running request stays on its original collection. The service's `TAYUNE_EMBEDDING_REVISION` must match the manifest. This is an operator-supplied artifact tag, not automatic cryptographic verification of model weights.

Revocation filters a document out of subsequent retrieval immediately. If revocations change before the final service check, its old-evidence answer is not delivered; cached results from a different revocation version are not replayed. Already emitted stream fragments cannot be recalled. Rebuild a candidate using `--exclude-document-id DOCUMENT_ID` to physically omit withdrawn material. Activation rejects versions containing revoked documents; rollback to unverified legacy is blocked while revocations exist. Previously completed answers are not rewritten. Failed staging can leave an inactive partial collection; it is never automatically published or deleted.

## Validation and Evaluation

At this update: 125 Python checks and 7 Node stream checks passed; the WordPress 1.4.0 asset build succeeded. Python checks include 32 business replay cases and tests that deliberately corrupt expected parameters/order to verify that the evaluator can fail. Existing gate fixtures now use the allowed shop host instead of an external example URL.

```bash
python -m tests.evaluation.run_tasks --output reports/core_replay.json
python -m tests.evaluation.run_tasks --baseline reports/core_replay.json
node --test tests/widget_stream.test.mjs
python -m scripts.audit_runs
```

The 32 English cases exercise the real graph, tool gateway, product-tool wrapper and answer gate against scripted model/catalog/knowledge fixtures. They cover product scope/budget, policies, brewing, dependencies, unavailable services, invalid inputs, unknown tools and citation failure. They do **not** measure the classifier, model semantic decisions, live retrieval relevance or factual correctness. A 32/32 execution-contract result is not 100% customer task success.

Live checks are opt-in and reject execution without both `--mode live --allow-paid` and `--max-cases 1..3`. They call the existing local server, not another Runtime. Only single-turn business cases are eligible; injected failures remain replay-only. Live output still needs human evidence review. Initial implementation made no live calls; a separate authorized three-question same-session acceptance was subsequently performed and recorded in LOCAL_ACCEPTANCE.md.

## Remaining Boundaries

Public memory/progress/cancel controls, real-provider acceptance of routing/retry behavior, real Milvus release activation/rollback and live business-quality evaluation remain unverified or undeployed. No multi-Agent collaboration, employee identity, authenticated order access, cross-session long-term profile, arbitrary-code sandbox or write-operation approval system is claimed. Those require actual business scope and authorization, not merely more framework components.

## Extractive Answer Contract

Added October24 after the real acceptance found that valid citation IDs did not prevent unsupported teaspoon amounts, scaled recipes or unsolicited claims. Prompt instructions alone had not prevented those additions.

The formal graph now supplies numbered evidence units from product details and retrieved text. Structured price, options and availability use deterministic labels; absent values remain unknown. Description/passage text is exposed as units rather than duplicated raw text. Each source has at most80 units, retaining each supplied nonempty line without accepting model-written substrings.

The model selects one to six units using only this schema:

```json
{"selections":[{"source_id":"S1","evidence_id":"E1"}]}
```

`grounding.py` rejects unknown IDs, empty/oversized selections, prose, Markdown fences and extra fields such as text, quote, title or URL. The server renders the selected original units, deduplicates repeated selections and supplies the registered title/link. Internal guides get a plain title without an invented URL. An overlong rendered answer is rejected, not cut mid-condition. Fixed clarification/failure codes still work without evidence. The old `check_answer` renderer remains for compatibility tests and fixed replies, but the formal graph does not use it to admit free-form factual answers.

An invalid final response gets at most one correction within the existing four-model/four-tool limits; exhausted budgets produce the existing fallback. No extra critic model or paid call is introduced by the checker. Agent tool selection and feedback remain intact; only final factual wording is constrained. JSON/SSE response shapes and the website plugin do not change. Audit events record `grounding_mode` without recording the answer text.

Tradeoff: this is extractive evidence selection, not a semantic fact checker or natural paraphrasing. It can be less conversational, cannot freely synthesize comparisons or calculate conversions, and may select irrelevant or conflicting units. A supplied line may depend on preceding context, contain an outdated claim, or be a partial chunk; matching it does not certify full context or truth. Source quality, retrieval relevance, contextual completeness and model selection still require evaluation. Unit/answer limits can trigger fallback on large material.

Validation:144 Python tests passed, including19 added grounding/graph regressions and the32 scripted business replays;7 Node stream tests passed. A separate replay invocation also passed32/32. These are contract checks using mocked dependencies, not model accuracy. They cover valid citations with invented prose, extra fields, unknown units, stable numbering, internal guides, conditions, bounds, repair and last-call fallback. Public fixtures were corrected to use the actual `text` and `is_in_stock` fields.

The latest code is not yet restarted or live-retested. The October23 real acceptance record remains unchanged evidence of the previous behavior; further paid verification needs separate authorization. No production data, Milvus release, daily limit, environment, tunnel or WordPress deployment was changed.

## Final-Answer Reservation

An October24 restart/retest reached the new source but failed before producing a final evidence selection: three empty keyword searches and one matching search used four reads; review plus three main calls consumed the physical model budget, with a product-detail request still pending. Only one of the authorized two questions was sent; the dependent follow-up was stopped. See [retest evidence](LOCAL_ACCEPTANCE.md#october24-retest--budget-exhaustion).

The subsequent gateway patch checks the shared physical counters on each attempt, including retries. For a main call with tools, the fourth physical model attempt or exhaustion of four tool attempts forces `tool_choice=none` and adds a finalization instruction. The model must select available relevant evidence or a fixed reply. If a provider still returns tool calls, they are rejected, not executed. Other roles and early calls remain unchanged; total budgets are not increased. `none` is a documented [DeepSeek Chat Completions option](https://api-docs.deepseek.com/api/create-chat-completion/).

Product-tool descriptions now favor one short literal type/name keyword or an empty-search catalog page, rather than combined alternatives/taste adjectives. That is guidance, not semantic search or enforced semantic correctness. Finalization can still safely degrade when evidence is missing; it does not manufacture the detail that an exhausted tool budget could not retrieve.

152 Python tests, including eight new gateway/SDK/graph cases, and seven Node tests pass. The SDK check uses MockTransport, not a real paid provider. The32 offline business replays still pass. The budget patch is not yet restarted or live-tested; the preceding real attempt does not establish extractive format adherence or completed shopping/brewing behavior.

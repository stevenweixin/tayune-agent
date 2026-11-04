# Isolated Engineering Evaluation

These runners never target the public website or production port 8011. They use
loopback 8012 and separate SQLite files under `reports/`. They do not change
production `.env`, website switches, quotas, or Milvus records.

## Capacity Foundation Verification

```sh
python -m tests.performance.verify_capacity_foundation --output reports/next_foundation
# Optional simulated3000-user ramp and spike, never a paid provider test:
python -m tests.performance.verify_capacity_foundation --output reports/next_foundation_load \
  --load-python reports/loadgen_20261104/bin/python
```

This fixture runtime binds the same validated capacity settings to the formal
SessionStore, TeaService, admission coordinator and classifier/RAG CPU queues.
It registers3000 sessions through the actual HTTP route; there is NO seeding route.
The8 checks verify registration, eight completed chats with only four active slots,
waiting, quota, task uniqueness, cleanup and idempotent replay. All external and
local model dependencies are fixtures. Isolated IP/daily limits are raised only
in this subprocess. Source hashes, invalid first-run input and raw outcomes are retained.

The existing --real-retrieval verifier loads actual local models/Milvus but stubs
every model role and the catalog. Its manually constructed Service intentionally
keeps max_waiting=0 for legacy rejection checks; it is not the new queued-default test.
Neither command changes production DB/.env/Tunnel or proves human answer accuracy.
See [capacity foundation implementation and results](../../docs/public/CAPACITY_FOUNDATION_20261104_ZH.md).

## 3000 Virtual Users and Real Answer Quality

```sh
# Separate load-generator venv, never install Locust into the business/course environment.
python -m venv reports/next_loadgen
reports/next_loadgen/bin/python -m pip install -r tests/performance/requirements.txt
python -m tests.performance.run_scale --output reports/next_scale/simulated_run2 \
  --load-python reports/next_loadgen/bin/python
# NEW paid authorization required: at most48 chats /192 physical attempts /estimated USD1.
python -m tests.performance.run_quality_load --output reports/next_scale/quality --paid
# Review all answers against frozen_cases.json, then save quality_review.json at next_scale root.
# Its cases contain profile/id/verdict/note, with verdict meets/partial/failed/clarification.
python -m tests.performance.analyze_scale_quality --input reports/next_scale
```

The scale runner first exercises the formal100-session cap with3000 registration
attempts. It then EXPLICITLY bypasses create() only to seed the isolated DB, so the
3000-user chat stages are NOT proof of successful public registration. Formal chat
admission stays4; CPU queue and per-session cooldown stay enabled. All dependencies
are fixtures. It runs short ramps10/50/100/300/1000/3000 and a one-shot3000-user barrier.
Only loopback8012 is allowed; benchmark endpoints never exist in the production app.

The generator streams measurements to JSONL and records achieved users; check those
files and summary, not only process exit codes. Locust exit1 is expected when429 is
marked business failure. Missing metadata/zero traffic is a harness failure, not a pass.
Keep the venv interpreter path WITHOUT resolving its symlink into the base interpreter.
The first load-generator startup failure is preserved in the November4 report.

The real runner compares the same24 regression cases sequentially and at4 concurrent
requests using actual local models, Milvus reads, catalog reads and DeepSeek. No mock
3000-user score is presented as real answer accuracy. Both workloads are on one
physical machine; no public Tunnel or distributed generator result is implied.
Real output keeps task IDs, actual tool results, SSE delivery integrity, latency and
cross-role physical usage. Cases/rubrics are frozen before requests. Review is not
independent human gold; order/cache/model randomness prevent causal attribution.
See the [Chinese scale and quality report](../../docs/public/SCALE_QUALITY_EVALUATION_20261104_ZH.md).

## Full Formal Agent Evaluation

```sh
# Read-only local retrieval comparison; no model-provider calls.
python -m tests.performance.run_components --output reports/next_full/components --language en
# Requires NEW authorization: 75 chat submissions, 300 physical attempts, estimated US$1 maximum.
python -m tests.performance.run_full_agent --output reports/next_full/agent --paid
# After reviewing ALL functional answers against frozen rubrics, create
# reports/next_full/assistant_review.json (review_kind, cases: id/verdict/note).
python -m tests.performance.summarize_full_agent --input reports/next_full
```

The paid runner uses the formal Service and CPUQueue, real local retrieval and public
catalog API. It runs 3 SSE smoke requests, 60 fixed functional requests, then bursts
of 4 and 8 different sessions against capacity 4. Model roles use deepseek-flash only
in this process. Session cooldown and per-question budgets remain enabled. Only the
isolated IP/daily limits are raised. It stops on unknown usage or aggregate budget;
it never auto-retries a whole question to replace a bad result. Shared paid budgeting
includes in-flight reservations and every model role. Startup refuses occupied 8012.

The summarizer is offline: it checks per-case review coverage, joins physical usage
to task IDs, verifies frozen source hashes, and emits analysis.json plus a private
per-case Markdown table. Verdicts are meets_rubric / partial / not_completed /
appropriate_clarification. Assistant review is not independent human gold accuracy.
Three short conversations do not exercise summary compression or long-term memory.
Raw results remain Git-ignored. See the [Chinese full report](../../docs/public/FULL_AGENT_EVALUATION_20261102_ZH.md).

## Formal Service Concurrency Verification (No Paid Model Calls)

```sh
python -m tests.performance.verify_production_concurrency --output reports/next_formal_concurrency
python -m tests.performance.verify_production_concurrency \
  --output reports/next_formal_real_rag --real-retrieval
```

Both commands exercise the production TeaService and CPUQueue through in-process
ASGI HTTP. The second loads actual local models and reads TAYUNE Milvus, but ALL
LLM roles/fallback and the catalog are replaced with fixtures before any chat.
Neither is a paid-provider load test or a semantic answer-quality measurement.
The 12 checks cover four active sessions, fifth/same-owner rejection, management
owner isolation, history, quota, idempotency and resource release.
Additional unit tests cover task recovery/cancellation and CPU timeout behavior.

Historical capacity experiment numbers belong to commit `02a56d6`. Current
experiment helpers reuse the formal Service coordinator; reproduce the original
frozen implementation from that revision, not by assuming later main is unchanged.
See [formal concurrency design](../../docs/public/CONCURRENCY_CONTROL.md).

## Bounded Concurrency Experiment

```sh
python -m tests.performance.run_capacity \
  --output reports/next_capacity_run --locust reports/loadgen/bin/locust
# Add --paid only after new authorization for up to 120 physical attempts / estimated US$1.
# Each run requires a NEW output directory; authorization is not renewed by changing that directory.
python -m tests.performance.summarize_capacity --input reports/next_paid_capacity_run
```

The non-paid command runs five 20-user, nominal 30-second simulated-dependency stages.
With `--paid`, it additionally runs the frozen real burst matrix sequentially on 8012.
It carries the remaining physical-attempt and cost budget between processes, stops on
unknown usage, and skips capacity 8 if earlier queued within-capacity bursts miss
the reference functional-proxy / latency threshold. It never kills an existing server.

Profiles compare single admission, wider admission with original fail-fast CPU,
and wider admission with a bounded shared CPU queue. Same-session chat ownership
is retained. Knowledge-tool pending capacity is also scaled in the experimental
process. Management mutation routes are DISABLED: this is not a production-ready
replacement for the service lock. No request is sent via the public Tunnel.

Fresh anonymous sessions alternate two fixed simple questions. Successful response
latency excludes 429 and degraded 200. The real test is a small burst experiment,
not steady-state QPS, human answer correctness, or a hardware maximum.
The summarizer consumes real results, so it requires a paid run output, not a
simulated-only output. Raw rows stay private under `reports/`.

See [Chinese capacity report](../../docs/public/CAPACITY_EXPERIMENT_20261102_ZH.md).

## Dependency-Free Baselines

Run the existing tracked Python unit suite and `node --test tests/widget_stream.test.mjs`.
The fixture business replay is `python -m tests.evaluation.run_tasks --output reports/contracts.json`.
These checks are not model accuracy measurements.

## HTTP Load and Failure Injection

Create a separate load-generator environment, not the application environment:

```sh
python -m venv reports/loadgen
reports/loadgen/bin/python -m pip install -r tests/performance/requirements.txt
python -m tests.performance.run_offline \
  --output reports/engineering_offline_run \
  --locust reports/loadgen/bin/locust --seconds 30
```

The runner refuses an occupied 8012 rather than killing an existing service.
It launches and stops only its own subprocesses. Locust also rejects non-loopback
targets. Load stages use 1/2/5/10/20 users, four rotating sessions each, and
0.2–0.4 seconds think time. The production Service lock and eight-second session
cooldown remain in force. Only the diagnostic instance's IP/daily limits are raised.

Real FastAPI, Service, Graph, tool/model gateways and SQLite run behind HTTP.
The model is simulated at 250 ms per call and the catalog at 50 ms per read;
classifier and retrieval fixtures are not real models. Do not call these results
DeepSeek throughput, production capacity, or real retrieval accuracy.

`*_requests.jsonl` is the raw chat ledger, including the final drained response.
`load_summary.json` separates accepted business completions, 429s, 5xx, and latency
of successful requests. RPS uses the nominal stage duration; ramp/drain overhead
is reported separately as wall time. Locust exits nonzero for expected 429 failures.
Its CSV snapshot can precede the last drained response; prefer the raw ledger for
completion counts and do not mix denominators between files.

## Read-Only Real Retrieval and Component Probes

```sh
python -m tests.performance.run_components --output reports/engineering_components_run
```

Loads existing local models and reads the configured TAYUNE Milvus collection.
No DeepSeek calls. Twelve synthetic source/literal-anchor labels are verified
against normalized documents and frozen before retrieval. Each query is embedded
once; dense and hybrid use the same vectors, approved corpus and six-candidate
budget. Rerank selects three from the hybrid six. Database timing excludes
embedding, and rerank timing excludes both. First observations are not discarded,
and each query is measured once, so percentiles are descriptive, not an SLA.

Document Hit@3 and literal-anchor Hit@3 are not exhaustive Recall@K. Labels are
not independently human-reviewed. Dense and hybrid are measured sequentially,
without randomized repetitions, so small timing differences are not a speedup claim.

## Paid End-to-End Probes

Only execute after explicit authorization of cost and request caps:

```sh
python -m tests.performance.run_real --output reports/engineering_real_run --paid
```

Maximum: three smoke chats plus sixty planned turns, 120 physical model attempts,
conservative estimated US$1. Unknown usage stops further attempts. Retries and all
model roles share the physical-attempt budget; SDK retries are disabled.
The test process overrides all roles to `deepseek-flash`, disables optional fallback,
and uses a private SQLite file. Production model selection is unchanged.

The paid runner uses real CPU classifier/embedding/reranker, Milvus reads, public
WooCommerce reads, and the official DeepSeek API. Only test IP limits are raised;
the execution lock, session cooldown and four-attempt request budget are retained.
It sends requests sequentially, not a real-provider concurrency stress test.
The first three chats measure verified SSE delivery, not native token streaming.

`frozen_cases.jsonl` is written and hashed before calls. Results include synthetic
questions, output, audit events, usage, elapsed time, and explicit checks. Session
tokens and API keys are not written to these reports; private SQLite checkpoints
and all raw reports remain gitignored. Do not publish them without inspection.

Strict contract checks are only proxies: nonempty response, allowed source domain,
budgets, public citations, refusal or scope redirection. Local evidence without a
public URL fails the public-citation proxy even when evidence exists. A correct
refusal can lack a tested keyword, and a grounded quotation can still fail to
answer the question. **Contract pass is not Answer Correctness.**

A known smoke failure can be continued with `--previous reports/previous_run`.
The frozen dataset hash and cumulative attempts/spend are carried forward;
unknown usage cannot be bypassed this way. Do not chain simultaneous runners or
reset the authorized budget. In the November 2 run, the first smoke stopped, its
catalog/filter evidence was inspected, then remaining probes continued without
changing production or model/tool logic. This was a supervised diagnostic decision,
not an uninterrupted deployment acceptance pass.

The November 2 evidence can be aggregated with:

```sh
python -m tests.performance.summarize --base reports/engineering_20261102
```

The public report is `docs/public/ENGINEERING_EVALUATION_20261102.md`.

## Targeted Fix Retest

The November 2 follow-up reads the five recorded CPU-contention tool-call pairs
from the private baseline checkpoints in SQLite read-only mode:

```sh
python -m tests.performance.replay_knowledge_failures \
  --baseline reports/engineering_20261102 --output reports/next_knowledge_replay
```

This runs real embedding/retrieval/rerank, with no paid model calls and no Milvus
writes. Execution success is distinct from nonempty results or answer usefulness.
The final follow-up evidence is `reports/retest_20261102/knowledge_replay_v2/`;
the earlier language-filter preflight in `knowledge_replay/` is not the accepted
version because it excluded the known legacy English metadata alias.

The follow-up paid run used the same frozen cases and a NEW authorized budget,
not `--previous` from the exhausted original 120-attempt run. Its report keeps the
last budget-curtailed request separate. For this evidence layout:

```sh
python -m tests.performance.compare_retest \
  --baseline reports/engineering_20261102 --retest reports/retest_20261102
```

The comparison excludes either batch's evaluation-budget-curtailed case before
reporting matched counts. This remains one stochastic run per version, not a
controlled statistical accuracy or latency-gain claim. Read the Chinese report
`docs/public/ENGINEERING_RETEST_20261102_ZH.md`, especially the semantic failures
that still passed the machine contract.

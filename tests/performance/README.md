# Isolated Engineering Evaluation

These runners never target the public website or production port 8011. They use
loopback 8012 and separate SQLite files under `reports/`. They do not change
production `.env`, website switches, quotas, or Milvus records.

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

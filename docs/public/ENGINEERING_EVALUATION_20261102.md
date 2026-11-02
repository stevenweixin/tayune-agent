# Engineering Evaluation: November 2, 2026

## 中文摘要

- 回归：169项Python、8项Node、32条固定业务重放及19条新HTTP契约均通过。
- 隔离HTTP压测：模拟依赖，20用户、30秒内50次业务完成、1800次429；单请求锁是主要限制，不能声称支持高并发。
- 真实依赖顺序评测：60条聊天、120次物理模型调用；P50为3.184秒、P95为5.321秒，保守估价US$0.10947。
- 45/60是非空、来源、预算、拒答/引导等规则代理检查通过数，不是答案正确率；14条降级中含最后一条被评测总预算截停的请求。
- 12条检索探针：三种方案均文档Hit@3为12/12、原文答案锚点Hit@3为11/12；没有测出混合检索或精排提升，不能写“召回提升25%”。
- 已定位：并行知识工具CPU竞争、商品问题误分类、中文证据直接面向英文顾客、无关问题与服务故障话术混用；本轮记录问题，未改生产行为。独立人工语义复核和真实供应商并发压测仍未完成。

## Scope and Conclusion

This is a measured baseline, not a high-concurrency certification or an answer-
accuracy claim. The application is a low-traffic, single-process pilot. It protects
bounded execution, but its global execution lock rejects concurrent work; adding
clients does not make it process proportionally more chats.

The evaluation did not change production configuration, website/plugin, daily
quota, model weights, or Milvus records. Port 8011 retained the same process.
Test HTTP servers on loopback 8012 and their SQLite databases were isolated and
stopped afterward. No classroom environment or database service was modified.

Application code was based on commit `d0ddbe1`; the only production-module change
was an optional Runtime session database path for isolation. Leaving it unspecified
preserves the existing production path. Runtime's recorded application source
fingerprint for the real run was
`ee60e46c69d1fe5b27801a678963535a95b8b4010f15e0ad4d2b181ddee21bd4`.

## Environment and Methods

- Local macOS machine, 32 GiB RAM, CPU inference, PyTorch using eight threads.
- Python 3.11; PyTorch 2.5.1; PyMilvus 2.4.9; LangGraph 1.0.9;
  langchain-openai 1.1.10; langchain-core 1.2.16.
- Locust 2.47.0 in its own virtual environment, not the application environment.
- Real retrieval used the existing `tayune_bgem3_hybrid_v1` approved collection,
  BGE-M3 dense/learned sparse embeddings, WeightedRanker(0.7, 0.3), and CPU rerank.
- Paid tests used `deepseek-flash` only in the isolated process. All 120 provider
  responses reported this alias. The official pricing/model page on the run date
  mapped it to DeepSeek-V4.1-Flash-1017; that version is documentation attribution,
  not a version identifier independently returned in every response.
- Production `.env` still contained `deepseek-chat`; it was not migrated. These
  results therefore do not certify the production model alias.

Model/pricing reference: [DeepSeek official documentation](https://api-docs.deepseek.com/quick_start/pricing/).

## Regression and HTTP Contracts

| Check | Actual Result | Meaning |
|---|---:|---|
| Existing Python baseline | 162/162 | Offline unit checks, not model accuracy |
| Final Python suite | 169/169 | Includes seven isolation/budget/metric checks |
| Node stream checks | 8/8 | Stream parsing, links, ordering, early EOF |
| Existing scripted business replay | 32/32 | Fixed model/tool fixtures |
| New local HTTP/failure contracts | 19/19 | Real HTTP and application control flow, simulated dependencies |

HTTP contracts covered origin denial, session IP limit, missing/unknown bearer,
oversized body/question, normal grounded response, idempotent replay without extra
model calls, request-key conflict, session cooldown, concurrent busy rejection,
injected authentication/network/invalid-JSON/tool failures, unknown tools,
guessed product IDs, verified SSE delivery, and recovery after clearing a fault.
Injected network failure used a simulated connection error, not an actual outage
at DeepSeek. A passing recovery probe is not an availability SLA.

## Isolated HTTP Load Test

Each stage lasted nominally 30 seconds. Model delay was **simulated at 250 ms** per
call; catalog reads at 50 ms. Classifier/retrieval were fixtures. FastAPI, Service,
Graph, gateways and SQLite were real. Diagnostic IP/daily limits were raised only
inside the test instance; global lock and eight-second cooldown were unchanged.
Each user rotated four sessions with 0.2–0.4 seconds think time.

| Users | Chat Requests | Business Completions | HTTP 429 | HTTP 5xx | Approx. Successful RPS | Successful P95 |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 58 | 12 | 46 | 0 | 0.40 | 609 ms |
| 2 | 137 | 27 | 110 | 0 | 0.90 | 604 ms |
| 5 | 400 | 43 | 357 | 0 | 1.43 | 606 ms |
| 10 | 885 | 49 | 836 | 0 | 1.63 | 601 ms |
| 20 | 1,850 | 50 | 1,800 | 0 | 1.67 | 611 ms |

Total: 3,330 chat requests, 181 business completions, 3,149 rejections. Service
instrumentation attributed 2,974 rejections to `busy`, and 175 to session cooldown.
The 20-user stage business completion rate was 2.70%, **not** 100% just because
there were no 5xx. Its all-request P95 was only 9.17 ms because quick rejections
dominated; successful-response P95 is the useful separate measurement.

Rates use nominal duration, include final drained chat responses, and are not
steady-state production throughput. First-stage process wall time was 39.63 s
including load-generator startup; later stages about 30.46–30.92 s. The Locust CSV
snapshot can precede the last drained response, so counts above use the final raw
chat ledger. Generator peak CPU stayed below 56% of one core in sampled readings;
test backend RSS stayed below 86 MiB because heavy dependencies were simulated.

**Finding:** admission policy/global serialization, not CPU saturation, dominated
this particular simulated workload. No throughput claim about DeepSeek, real
embedding, public Cloudflare, mobile clients, or a production multi-instance setup
can be inferred from it. A 30-second run also cannot establish long-run stability.

## Real Agent Probes

Authorized cap: at most 63 chat requests including smoke, 120 physical model
attempts, estimated US$1, stop on unknown provider usage. The initial product smoke
returned a safe fallback and stopped that attempt. Its evidence was inspected;
the same budget and frozen dataset were then continued in a fresh isolated DB.
No service/model/tool logic was changed between those two real runs.

Actual total:

- **60 chat HTTP requests**, all returning HTTP 200; this does not imply business success.
- **120 physical model attempts**, 343,465 input tokens and 5,357 output tokens.
- **US$0.10947 conservative estimate**, charging every input at the peak cache-miss
  rate ($0.30/M) and output at $1.20/M. This is not a provider invoice; cache/off-peak
  billing may be lower. All responses supplied usage.
- All-chat **P50 3.184 s; P95 5.321 s; max 6.666 s**. The mixture includes short
  refusals, fallbacks and three SSE chats. It is not positive-query latency alone.
- The 34 positive-query strict-contract passes had **P50 3.342 s; P95 5.321 s**.
- Three SSE smoke requests: first verified text segment P50 3.949 s, range limited
  by a sample of three. This is **not native model TTFT**; delivery starts after
  answer verification.
- 53 successful tool results, five `knowledge_unavailable` tool failures.
- 14 degraded replies, including the final request curtailed by the evaluation
  budget. Three planned turns were not sent after the physical-attempt cap.

| Synthetic Category | Requests Sent | Strict Contract Passes |
|---|---:|---:|
| Smoke | 3 | 2 |
| Product | 12 | 11 |
| Knowledge | 24 | 20 |
| Restricted | 12 | 11 |
| Out of scope | 6 | 0 |
| Multi-turn turns | 3 | 1 |
| Total | 60 | 45 |

**45/60 (75%) is a strict rule-proxy pass rate, not Answer Correctness.** Rules
check nonempty text, allowed public source URLs, budgets, explicit refusal or
redirection. Internal-only evidence can fail the public-source proxy even when
the answer is grounded. Conversely, a quoted source can be irrelevant or incomplete
and still pass. Six out-of-scope requests were safely not answered and executed no
tools, but failed the expected scope-redirection wording because production policy
maps unrelated requests to the generic unavailable reply. One restricted request
also safely fell back without matching the explicit refusal wording.

The dataset was synthetic, with no independent human gold review. Tests were
sequential over local HTTP and real dependencies, **not a real-provider concurrent
load test**. No RAGAS/independent LLM judge score was produced. Multi-turn coverage
is incomplete: the oolong first turn failed to establish a selection, and the last
jasmine first turn hit the overall evaluation cap. It would be incorrect to claim
successful complete multi-turn acceptance from the single follow-up proxy pass.

## Real Retrieval Ablation and Component Timing

Twelve synthetic single-source probes had source titles and literal answer anchors
checked against normalized documents, then frozen before retrieval. They used the
same once-computed query vectors and approved corpus. Dense/hybrid recalled six;
rerank selected three from hybrid six. No source-type filter was used. Labels have
not received independent human review, and multiple valid sources were not exhaustively
annotated. These are **Hit@K probes, not comprehensive Retrieval Recall**.

| Method | Document Hit@3 | Literal Answer-Anchor Hit@3 |
|---|---:|---:|
| Dense only | 12/12 | 11/12 |
| Hybrid | 12/12 | 11/12 |
| Hybrid + Rerank | 12/12 | 11/12 |

No gain was demonstrated on this small/easy set. It does not support a “25%+
recall improvement” claim. The shared miss was the literal mug-brewing ratio
anchor in the Pu-erh brewing document: the document was retrieved, but that answer
anchor was not in the top three returned chunks. This distinguishes document
retrieval from locating an answer-bearing passage.

| Stage | Observations | P50 | Observed P95 |
|---|---:|---:|---:|
| Classifier | 12 | 8.14 ms | 19.85 ms |
| Query embedding | 12 | 210.29 ms | 383.08 ms |
| Dense DB read, excluding embedding | 12 | 266.48 ms | 459.16 ms |
| Hybrid DB read, excluding embedding | 12 | 221.57 ms | 404.72 ms |
| Rerank six candidates to three | 12 | 824.09 ms | 1,155.27 ms |

First observations were retained; one observation per query. With twelve samples,
nearest-rank P95 equals the maximum. Sequential measurement, cache/order effects
and no randomized repeats mean these numbers are descriptive, not evidence that
hybrid is faster than dense. They do show rerank is a material part of this CPU
pipeline and should not be treated as free. Model loading was 6.72 s in the
component run; process RSS after probes was about 3,545 MiB.

## Confirmed Findings and Next Work

1. **Global execution lock rejects concurrent users.** The existing low-resource
   design bounds work but does not support a high-concurrency claim. Before changing
   it, specify admission/queue limits and serialize actual CPU work safely; do not
   simply remove the lock or disable cost controls.
2. **Parallel knowledge tools conflict at CPUWorker.** Five real tool failures
   occurred when two knowledge calls started together: the second failed immediately
   with CPU busy while the first completed. This is confirmed by the tool audit and
   knowledge logs. Queue/serialize CPU-bound knowledge calls within the request's
   deadline, with a bounded wait; retain parallel non-CPU reads where appropriate.
3. **Classifier generalization gap.** Among 54 first-turn category-labelled probes,
   labels matched 45 (83.33%). Eight of twelve valid product questions were marked
   restricted; review corrected them but consumed latency and model budget. One
   hidden-prompt request was labelled out-of-scope instead of restricted and safely
   fell back. This small new distribution must not be conflated with the earlier
   synthetic holdout score. Add independently reviewed command/paraphrase cases
   before changing routing thresholds or retraining.
4. **Source fidelity does not guarantee a useful English answer.** “What is gongfu
   brewing?” returned Chinese internal-guide excerpts; no hallucination is needed
   for this to be a poor customer answer. Separate relevance, answer coverage,
   language and evidence verification in future human-reviewed labels. Local
   evidence should not be considered missing solely because it lacks a public URL.
5. **Scope rejection and service failure are conflated.** Six unrelated questions
   returned generic unavailability by current prompt policy. Introduce an explicit
   safe scope-redirection contract rather than treating them as dependency outages.
6. **Budget/catalogue fallback needs a better explanation.** Oolong-under-$30
   attempts produced no qualifying result from the checked page and strict
   all-options budget filter; the fallback did not explain that limitation. Preserve
   the price guard and one-page limitation; do not substitute unrelated teas or
   claim no matching product exists across the entire catalogue.
7. **A follow-up cannot be credited when the selection premise failed.** Future
   replay labels must check the first-turn selected product and the follow-up's
   actual product identity, budget and brewing evidence, not only presence of links.

This pass records findings rather than claiming those production issues have been
fixed. No load/admission architecture, model, prompt, classifier, or website change
was deployed as part of the evaluation.

## Evidence and Reproduction

See [runner instructions](../../tests/performance/README.md), [load runner](../../tests/performance/run_offline.py),
[real runner](../../tests/performance/run_real.py), [component runner](../../tests/performance/run_components.py),
and [budget checks](../../tests/unit/test_engineering_evaluation.py).

Local, gitignored evidence is under `reports/engineering_20261102/`: unit logs,
`contract_baseline.json`, `offline_v1/`, `real_v1/`, `real_v2/`, `components_v1/`, and
`aggregate.json`. Raw evidence includes private test checkpoints and must not be
published wholesale. Representative frozen hashes:

- 60-turn dataset: `b1cbf26c309addcca9a57f63492d42ffffbc1890d67c52eb1b2dc0d73f9fe277`
- 12 retrieval labels: `e0297d4fdbf0e8a99c0b2fa94252e9289ca9b3370ed30c132e7c29c3ce046e29`
- First real results: `9e813d26904bb93a3f1eee7804364f59129962208f7805259c9b0c79d44b831c`
- Continued real results: `efbfcce8cfa12b181d33db0d84b0f0e3b682bb502811d74462e15b0e3174459c`

For interviews, a defensible statement is: “I separated dependency-simulated HTTP
load from real-provider end-to-end probes, measured success and rejection separately,
and found the single-request lock and CPU tool contention. Real sequential probes
had a 5.32-second observed P95 in this run; that is not a high-concurrency SLA or
an answer-accuracy percentage.”

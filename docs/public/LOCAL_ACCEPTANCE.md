# Local API Acceptance — 2026-10-23

## Scope and Version

The operator restarted the backend after commit `2cef14d`. Readiness returned 200;
the new local management route returned 401 without a session, rather than the old
404. Three explicitly authorized chat requests were sent to loopback, in one new
session. No extra chat retries were made. The WordPress plugin was not uploaded,
Milvus data was not changed, and public ingress permissions were not expanded.

This verifies the running local backend, not the public browser/widget integration.
The existing tunnel may already route website requests to this restarted backend;
it was not disabled or reconfigured. Widget 1.4.0 remains undeployed.

## Observed Results

| Case | Time | Model calls | Tool attempts | Observation |
| --- | --- | --- | --- | --- |
| Suggest one light, floral tea under USD30 | 8.013s | 4 | 3 | Completed with product links; jasmine USD19.90 and stock matched the public Store API. One classifier-restricted result was recovered by intent review. |
| How should I brew that tea in a mug? | 17.899s | 4 | 4 | Resolved jasmine from the same conversation; fetched product details and knowledge anew; returned the product recipe with inline links. |
| What is your return policy? | 4.022s | 2 | 1 | Policy retrieval succeeded; opened-tea, parcel-issue and contact wording matched the returned passages and current public policy page. |

All three returned HTTP200, `completed`, and `degraded=false`. Task audit was
readable by the owning session; model/tool counts and known token totals matched
the audit events. History contained three human/assistant pairs. The daily request
counter increased by exactly three; this does not imply provider billing is exactly-once.

Known usage: 34,172 input tokens and 1,048 output tokens, across ten physical model
calls. No USD invoice amount was inferred. No retry, failover, summary-trigger or
recovery path was exercised by these paid requests.

## Quality Gaps — Not Fixed by This Acceptance

1. The first request was classified restricted with score0.901. Public intent review
   recovered it, but consumed one of four available model calls. This is one observed
   false positive, not a classifier accuracy measurement.
2. The first answer added unsolicited brewing guidance and a second product despite
   a one-option request. Chrysanthemum caffeine-free wording exists on the current
   public product page, but was not explicitly present in the tool data returned for
   that product during this request. External corroboration is not the same as a
   complete in-turn evidence chain.
3. The mug follow-up added “heaped teaspoon” and a 350–400ml / 4–5g example not present
   in the supplied passages or product instructions. The core 2–3g/200ml, 80–85°C,
   2–3minute recipe was supported, but those extra formulations were not. A registered
   citation does not prove each sentence or conversion is grounded.
4. The follow-up exhausted both four-call limits, leaving no repair headroom. The
   first exact-name search was empty before a shorter keyword search succeeded.

**Conclusion: the three execution paths completed, but full answer-grounding
acceptance did not pass.** These are real smoke cases, not a percentage accuracy
benchmark. No model/prompt/runtime change was made during this acceptance.

## Free Checks

Nine checks against two disposable local sessions passed: explicit preference write,
correction, isolation, foreign-task denial, forwarded-management denial, unknown
private-field rejection, owned deletion, token invalidation and preservation of the
other session. Only those two disposable sessions were deleted. The three-question
test session follows normal retention. These checks did not call the model.

125 Python regressions and 7 Node stream-protocol checks passed again. Fault/retry,
idempotency, recovery and knowledge-release regressions used temporary databases and
simulated dependencies, not induced production failures. The paid chat requests used
JSON, so they do not constitute an SSE or mobile-browser acceptance test.

Private generated evidence is retained locally under `reports/` and excluded from
Git. Next priorities are evidence-bound answer generation and tighter lookup/call
selection, followed by separately authorized real retests. Actual Milvus release
activation/rollback and WordPress1.4.0 deployment remain outside this run.

## October24 Retest — Budget Exhaustion

The operator restarted after `e0e3866` and authorized at most two real questions in
one session. Readiness returned200, and the running code hash matched the committed
app sources. Only the first question was sent: “Suggest one light, floral tea under
$30 USD for me to consider.” It returnedHTTP200 in5.508s, but `degraded=true` and no
product answer. The dependent mug follow-up was not sent, and no chat was retried.

Audit: the classifier again returned restricted0.901, recovered by PUBLIC review.
The main model searched “floral oolong” and “jasmine white tea”, both empty, followed
by “oolong” (empty) and “jasmine” (one matching USD19.90 product, ID635). It then
requested detail after all four tools were used. Review plus three main calls had
also used all four physical model attempts. No final-selection response was produced;
there was no evidence-selection format rejection. This is not a daily-rate-limit,
API-key or Milvus-connectivity failure.

Known usage:8584 input and377 output tokens, four model calls and four executed tool
reads. The rejected fifth tool request was not executed. Daily count increased0to1,
and history contains one human/assistant pair. Only this test task's private evidence
was inspected. Its generated report remains ignored under reports/; no session
credential is persisted there. No plugin, Milvus or quota setting was changed.

Subsequent source fix: the model gateway uses the shared physical counters, including
review/summary/retries, to force `tool_choice=none` for the last main-model call or
when tool attempts are exhausted. It asks for current evidence selections or a fixed
reply, not more tool requests. Retries recompute the condition; an ignored none is
rejected without executing tools. Product prompts favor a short literal keyword or
a catalog page instead of speculative combined taste terms. This prompt change is
not a semantic search implementation or a guaranteed retrieval improvement.

152 Python checks and7 Node checks passed;32 offline contract replays remain passing.
Eight new tests cover finalization, unchanged early calls, tool exhaustion, retry,
option precedence, ignored none, actual SDK request construction with MockTransport,
and a graph with review plus four reads. No further real-model request was made.
The budget fix requires a new restart and authorized retest. Neither the shopping
task nor the extractive format is marked live-accepted by this failed attempt.

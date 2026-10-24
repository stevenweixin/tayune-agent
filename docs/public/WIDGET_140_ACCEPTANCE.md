# WordPress 1.4.0 Deployment and Acceptance

Date: 2026-10-24. Status: **deployed; partial live acceptance**.

## Deployment

The operator authorized updating the existing plugin and sending at most three real
website questions. WordPress confirmed replacement of TAYUNE Tea Companion1.3.1
with1.4.0 and reported a successful upgrade. The anonymous homepage subsequently
loaded `tea_companion.js?ver=1.4.0`; no extra cache purge was necessary.
The uploaded ZIP matches the SHA256 and three-file allowlist in
[the preflight record](WIDGET_PREFLIGHT.md).

Chat remained enabled. No backend code, runtime, environment, budget, tunnel or
Milvus configuration was changed. Local and public readiness returned ready.
No paid service or subscription was added. The1.3.1 rollback archive remains local.

## Exactly Three Website Submissions

All submissions used the anonymous website widget and its public HTTPS API in one
conversation. There was no automatic or manual resubmission.

| Submission | Observed result |
| --- | --- |
| Show me one in-stock jasmine tea under $25 USD. | Completed without degradation. Jasmine500g, USD19.90 and in-stock status matched the inspected public product page. The title itself linked to that product. Three physical model calls and one product-tool read;5571 input/135 output tokens. |
| How should I brew that tea in a mug? | The browser displayed its generic network/unreachable notice and restored the draft. No corresponding task registration or classifier/model/tool entry was found in the application records. No answer was received. The exact browser/network failure cause was not captured; it must not be described as a confirmed timeout, quota rejection, model failure or fixed defect. |
| What is your return policy? | Completed without degradation. One policy lookup recalled six candidates and reranked three passages; the answer's opened-tea rule, parcel remedies and contact instructions matched the inspected public policy page. The policy title was an inline link. Two physical model calls and one knowledge-tool read;7331 input/102 output tokens. |

The two registered tasks used five physical model calls and two tool attempts in
total, with12902 input and237 output tokens. The persisted UTC-day request count
was4 after the product answer and5 after the policy answer; only these two new
tasks were found. This is an application-side accounting observation, not an
independent provider billing audit. The three-submission authorization is exhausted.

Policy classifier-to-completion log span was about8.01s; product span about3.50s.
These are not browser first-text or end-to-end latency measurements. Both successful
cases used fewer than four model calls, so they do not exercise forced last-call
finalization. The policy case exercises the real knowledge path, not just WooCommerce.

## Interface and Independent Search

- Both completed answers rendered title links inside the answer, without a separate
  bottom source list. The full completed text was captured; a public-network
  intermediate SSE frame/cadence was not captured during this run.
- The follow-up error preserved the question; there was no automatic retry. The
  next explicit question remained usable and completed in the same conversation.
- The393x852 viewport placed the panel at x12..381 and y86..706; document width
  remained393px, without horizontal overflow. This is not physical iPhone Safari,
  keyboard or address-bar acceptance.
- Ordinary Search submitted `jasmine` independently and returned the correct product.
  The public product page and policy page loaded normally. No cart mutation,
  checkout transaction or customer account operation was performed.
- No browser warnings/errors were captured by the available console log collector.
  This does not explain or disprove the observed network failure.
- Installation, desktop/mobile answer and failure screenshots remain in ignored
  local reports; they contain no published session credentials.

## Remaining Verification

One failed follow-up remains an unresolved live observation, not a passed case.
Earlier local two-turn success is recorded separately and does not replace this
failed public run. A future authorized retest should inspect the request/network
status and task registration together before attributing the failure. Do not
automatically retry, reset budgets, widen tunnel paths or rebuild data to hide it.

The plugin is available as a small public pilot, not an all-cases acceptance or
availability guarantee. Original-device Safari confirmation and a public SSE
intermediate capture remain pending. Existing162 Python/8 Node preflight checks
are offline evidence, not a new test run or an online accuracy percentage.

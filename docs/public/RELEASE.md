# Public Pilot Release

Updated: 2026-10-22. Status: small public pilot, widget 1.3.0 deployed.

The deployed site uses widget 1.3.0 with chat enabled. The existing tunnel forwards the exact `/api/session` and `/api/chat` paths in addition to health checks. Activation was explicitly approved; no new paid service was added.

## 1.3.0 Incremental Delivery Update

The same chat endpoint negotiates JSON or SSE using `Accept`. After the existing answer gate completes, `meta`, structured `delta` and `done` events progressively deliver the validated answer. This is not native LLM token streaming and does not shorten the initial retrieval/generation wait. Partial unvalidated drafts are not released.

Product/document titles are inline links resolved from registered source IDs, never model-invented URLs. Legacy citations remain supported at their sentence location. The separate bottom source list is removed. The widget renders React text/link nodes, permits only HTTPS shop URLs, and reports interrupted streams without automatic retries.

84 Python and7 Node checks passed, alongside PHP syntax and production build checks. A fixed-data browser fixture showed partial text while streaming and complete inline links afterward; it made no model requests. Exactly one separately authorized question was sent through the anonymous real website after deployment: two loose-leaf teas below USD50 were returned, with Jasmine USD19.90 and Dong Ding USD45.90 linked in the body. The answer was not degraded, and the daily counter changed17 to18. The live capture occurred after completion, so no public-network first-text latency or chunk cadence was measured. This is not a general accuracy benchmark. The original two-case pilot acceptance below remains historical evidence for1.2.0.

## Scope

One tea-service Agent with product search/detail, approved knowledge lookup and contextual follow-ups. It does not access private orders, execute payments/refunds, publish content or modify products. It is not a multi-agent system.

The existing local computer hosts the FastAPI backend, CPU models, isolated Milvus and SQLite. Cloudflare Tunnel provides the fixed HTTPS API hostname. No new cloud-server subscription or automatic-start service was added. The computer must remain awake and connected; there is no24-hour availability claim.

## Basic Controls

- Backend binds to loopback only, one process. Milvus is not exposed by the tunnel.
- Tunnel matches health endpoints and exact session/chat paths only; demo pages and OpenAPI remain blocked.
- Public requests require random anonymous session credentials, with24-hour idle expiry. These are not customer account authentication.
- Default global budget:20 chat requests per UTC day; SQLite persists this through restarts. A request can include multiple model/tool calls, so this is not a fixed currency cap.
- IP limits:5 session creations and10 chat attempts per minute. Accepted messages are limited to500 characters and8KB request bodies; a session waits8 seconds between questions.
- The graph accepts at most4 model calls and4 tool attempts. Restricted-intent review uses one of the model calls; conversation summarization has its own bounded call path.
- Secrets remain on the backend. Browser messages are rendered as text, with verified shop links. No automatic frontend retry.
- Important information should be verified with the shop. Privacy notice identifies DeepSeek and session expiry; users are asked not to submit personal/payment details.

Origin checks and CORS are not authentication. These controls limit costs and scope but do not prevent distributed abuse or guarantee claim-level factual correctness.

## Verification

70 formal offline checks passed: answer gate14, product/internal evidence19, local widget/timeout6, API entry9, reranker windows10, intent review/service12. These do not load production models or measure real-answer accuracy. PHP syntax and the production frontend build passed.

Three authorized local widget cases passed after the evidence fix; details and limits are recorded in [EVIDENCE_FIX.md](EVIDENCE_FIX.md).

After activating the pilot, exactly two authorized questions were submitted from an anonymous browser at `https://tayunetea.com/`, using the deployed widget and public HTTPS API:

| Case | Observed path and result |
| --- | --- |
| Loose-leaf oolong below USD 50, excluding teaware | One successful product search; Dong Ding 500g at USD 45.90, in stock and purchasable, with a product link. The inspected current product page matched these details. Complete catalog recall was not assessed. |
| Return policy for opened tea | One successful policy knowledge lookup, six recalled candidates and three reranked passages. The answer stated that opened tea cannot be returned and linked the policies. The published shipping/returns text supports the stated terms. |

Both completed with `degraded=False`. Classifier-to-completion log spans were approximately 4.30s and 8.90s; these are not browser end-to-end benchmarks. The request summary's `tools` value includes non-tool trace entries; actual tool counts above use tool-stage records.

The persisted daily chat counter increased from 9 to 11, and testing stopped after the second question. No browser warnings/errors were captured. Public readiness and CORS preflight returned 200; chat without a session returned 401 with `no-store`; public demo and OpenAPI returned 404. The visible widget identifies DeepSeek, session expiry and the privacy page.

This is acceptance of the initial low-traffic request path, not a claim of general answer accuracy, full security coverage or sustained availability. Broad safety evaluation and offline-device behavior were not live-tested in this release; existing offline guards and fallback checks remain in place. No third-party subscription, database rebuild or classroom-service change was made.

## Operator Steps

1. Start the TAYUNE-specific Milvus, then run`python -m app.main` from the project root using its own environment. Confirm local readiness.
2. Configure the existing private tunnel using`deploy/cloudflare.example.yml` as a template; never publish credentials. Validate before restarting only this project's connector with`zsh deploy/run_tunnel.sh`.
3. Verify public readiness, CORS preflight, missing-session rejection and default404 for demo/OpenAPI.
4. Update only the TAYUNE Tea Companion plugin, flush existing page cache, and inspect an anonymous website visit.
5. Execute the explicitly authorized small question set and record actual answers, sources, failure behavior and deployed version.

Do not restart classroom services, recreate collections, change site/email DNS or upload model/data/secret files as part of the plugin deployment.

## Rollback

Deactivate TAYUNE Tea Companion in WordPress to remove the widget. Reinstalling1.1.2 restores the frontend-only coming-soon behavior. To block the API while keeping the public site unchanged, restore the private tunnel's health-only ingress and restart this project's connector.

No checkout/payment transaction or independent US-client latency test has been performed. Long answers, keyword search coverage, model errors and intermittent upstream availability remain iteration items rather than claims of solved production quality.

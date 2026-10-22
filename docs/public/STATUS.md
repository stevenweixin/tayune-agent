# Development Status

Updated: 2026-10-22.

The website widget frontend is deployed with chat explicitly disabled. The Agent backend is not publicly released.

## Implemented

- Document processing, BGE-M3 embeddings and isolated Milvus storage.
- Dense/sparse retrieval and a reranking integration.
- Query classifier inference and conversation summary integration.
- LangGraph model/tool feedback loop with bounded attempts.
- WooCommerce read-only product tools.
- SQLite sessions, request budgets and FastAPI endpoints.
- Local browser demo and a deployed WordPress floating tea companion widget with isolated styles, motion controls and a clearly labelled coming-soon state.
- Bounded token-window reranking for long candidates; local model and Milvus checks passed.
- Product scope and USD budget filtering, with citation selection from registered source IDs.
- One bounded, tool-free intent review for classifier-restricted requests; public requests can continue to the existing graph.
- A final-answer citation gate with fixed source-free clarification replies and at most one correction cycle within the existing model/tool budgets.

## Validation and Remaining Work

- The first three authorized local browser smoke cases exposed an unsourced brewing follow-up. After implementing the citation gate, three separately authorized retests returned a product/budget answer and an opened-tea policy answer matching the inspected public pages. The brewing follow-up invoked knowledge tools but safely degraded after invalid/missing citations. The business task remains unresolved; these six smoke cases are not an accuracy benchmark.
- Subsequent inspection identified URL-less internal guides and omitted product-description text as evidence-path gaps. Both were fixed: real chunk IDs identify internal evidence, while product detail retains the supplied brewing text. Three separately authorized local widget cases then passed: a budget product query, its product-specific mug-brewing follow-up and an internal ripe-pu-erh guide. See [evidence fix and retest](EVIDENCE_FIX.md). These cases are not an accuracy benchmark.
- Fourteen citation-gate checks passed against the formal modules using simulated models/tools, alongside the six existing local-page/timeout checks. These do not establish semantic grounding of every claim.
- Backend wait budget is85s versus the widget's95s. A loopback-only widget test page and six offline timeout/isolation checks are available; cancellation does not forcibly stop synchronous CPU threads.

- 32 targeted offline checks passed against the formal intent review/service, product/source selection and reranking code. These use simulated external dependencies, not live semantic evaluation.
- Local backend logs on October 21 show two PUBLIC review decisions followed by successful product queries, and five RESTRICTED decisions without tool execution.
- Logs omit question and answer text: these records do not independently establish the exact wording, every product detail, or general safety accuracy.
- The original cause of the first policy lookup failure remains unresolved, although later local policy regressions succeeded.
- Prompts discourage unnecessary lookups; bounded attempts remain a possible fallback condition, not a solved optimization problem.
- Source registration prevents displaying unused candidate links or accepting unknown source IDs; it does not prove that each answer claim is supported.
- Reranker evidence quality, broader business cases, failure modes and live website chat still need evaluation. Frontend-only checks covered opening/closing, ordinary product search, an excluded account page and a390px viewport without horizontal overflow.

The local readiness endpoint returned ready on October 21. This is not acceptance of public deployment. See [fix log](FIXLOG.md).

Release preparation on October 22 reran 70 formal offline checks: 14 answer-gate, 19 product/internal-evidence, 6 local-page/timeout, 9 API-entry, 10 reranker-window and 12 intent-review/service checks. Dependencies were simulated where appropriate; no paid model requests were made for these offline checks. The 1.2.0 plugin package and exact-path tunnel template are ready. Public deployment has not yet been switched on; see [release status](RELEASE.md).

## Deployment and Data

The full request path has been exercised locally. A fixed HTTPS tunnel now exposes only the two health endpoints; chat/session APIs, the demo and OpenAPI remain blocked at the tunnel.
The widget is installed on tayunetea.com with chatEnabled=false: input/send are disabled, no chat requests are sent, and shop/contact links remain available. The public chat path still returns404. Backend chat deployment and end-to-end website acceptance are pending. The connector runs on the local computer, without an installed startup service or an availability guarantee.
Model weights, private source documents, generated datasets, logs and session databases are excluded.
No production availability, answer accuracy or commercial impact is claimed.

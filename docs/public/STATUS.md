# Development Status

Updated: 2026-10-24.

Frontend1.4.0 is deployed; anonymous visitors load the1.4.0 asset. Exactly three authorized website submissions produced two completed answers (product and policy) and one network-failed brewing follow-up without a corresponding application task. No automatic retry occurred. Inline title links,393px layout and ordinary search were checked. This is **partial live acceptance**, not an all-cases pass. See [deployment evidence and unresolved failure](WIDGET_140_ACCEPTANCE.md). The earlier162 Python/8 Node preflight checks remain offline evidence; they were not rerun as part of deployment.

The operator restarted the budget patch `b0b1fc5`. **One local real-model shopping-to-brewing conversation completed with evidence-matching output.** Shopping took5.784s; a separately authorized original-session follow-up took4.777s and used the jasmine product's current brewing text without invented teaspoon/large-mug conversions. An earlier immediate follow-up hit the existing eight-second session interval; it called no model, and was resent only after explicit new authorization. Both successful turns used three model calls and two tool reads each, so forced last-call finalization remains offline-verified only. Existing152 Python checks,7 Node checks and32 offline replays are not accuracy measurements. New website/SSE/mobile and broader business acceptance remain pending. See [completed two-turn evidence](LOCAL_ACCEPTANCE.md#october24-authorized-original-session-follow-up--completed) and [contract/tradeoffs](CORE_RELIABILITY.md#extractive-answer-contract).

Earlier October23 state: the1.4.0 reliability backend candidate was running after an operator restart, while the widget had not yet been deployed. It added bounded task plans/checkpoints, session-scoped idempotency, model/tool gateways, explicit preferences and local management, minimized execution audit,32 English contract replays and immutable knowledge releases.125 Python checks,7 Node checks and the widget build passed. Three separately authorized local real-model questions completed; manual review found unsupported answer elaborations. Later grounding changes and acceptance records are linked above. Nine free local management checks passed. No real Milvus release operation was performed. See [local acceptance history](LOCAL_ACCEPTANCE.md) and [implementation scope](CORE_RELIABILITY.md).

The website widget1.4.0 and Agent API are available as a small public pilot. The initial two website smoke cases and one separately authorized inline-link update case passed on October22. A mobile orb compatibility patch is included; original iPhone confirmation remains pending. The backend remains locally hosted, without an availability guarantee.

## Implemented

- Document processing, BGE-M3 embeddings and isolated Milvus storage.
- Dense/sparse retrieval and a reranking integration.
- Query classifier inference and conversation summary integration.
- LangGraph model/tool feedback loop with bounded attempts.
- WooCommerce read-only product tools.
- SQLite sessions, request budgets and FastAPI endpoints.
- Local browser demo and a deployed WordPress floating tea companion widget with isolated styles, motion controls and real chat enabled.
- Bounded token-window reranking for long candidates; local model and Milvus checks passed.
- Product scope and USD budget filtering, with citation selection from registered source IDs.
- One bounded, tool-free intent review for classifier-restricted requests; public requests can continue to the existing graph.
- A final-answer citation gate with fixed source-free clarification replies and at most one correction cycle within the existing model/tool budgets.
- The running formal graph replaces free-form cited final answers with validated evidence-unit selections; fixed replies and the correction budget remain. Local shopping/brewing and public product/policy cases completed, but broader semantic relevance and the failed public follow-up remain evaluation items.
- SSE delivery of the completed, validated answer and structured inline title links. This is not native model token streaming; initial generation wait is unchanged. Legacy JSON clients remain supported.

## Validation and Remaining Work

- The1.3.0 update passed84 Python and7 Node offline checks. A fixed-data browser fixture demonstrated incremental display; one real website question verified completion and inline product links after deployment. No public-network intermediate frame or first-text benchmark was captured. See [release details](RELEASE.md).

- The first three authorized local browser smoke cases exposed an unsourced brewing follow-up. After implementing the citation gate, three separately authorized retests returned a product/budget answer and an opened-tea policy answer matching the inspected public pages. The brewing follow-up invoked knowledge tools but safely degraded after invalid/missing citations. At that stage the brewing task remained unresolved; the subsequent fix and retest are described below. These six smoke cases are not an accuracy benchmark.
- Subsequent inspection identified URL-less internal guides and omitted product-description text as evidence-path gaps. Both were fixed: real chunk IDs identify internal evidence, while product detail retains the supplied brewing text. Three separately authorized local widget cases then passed: a budget product query, its product-specific mug-brewing follow-up and an internal ripe-pu-erh guide. See [evidence fix and retest](EVIDENCE_FIX.md). These cases are not an accuracy benchmark.
- Fourteen citation-gate checks passed against the formal modules using simulated models/tools, alongside the six existing local-page/timeout checks. These do not establish semantic grounding of every claim.
- Backend processing budget is85s; the deployed1.4.0 widget waits115s to include bounded SSE delivery and margin. A loopback-only widget test page and offline timeout/isolation checks are available; cancellation does not forcibly stop synchronous CPU threads.

- 32 targeted offline checks passed against the formal intent review/service, product/source selection and reranking code. These use simulated external dependencies, not live semantic evaluation.
- Local backend logs on October 21 show two PUBLIC review decisions followed by successful product queries, and five RESTRICTED decisions without tool execution.
- Logs omit question and answer text: these records do not independently establish the exact wording, every product detail, or general safety accuracy.
- The original cause of the first policy lookup failure remains unresolved, although later local policy regressions succeeded.
- Prompts discourage unnecessary lookups; bounded attempts remain a possible fallback condition, not a solved optimization problem.
- Source registration prevents displaying unused candidate links or accepting unknown source IDs; it does not prove that each answer claim is supported.
- Reranker evidence quality, broader business cases, failure modes and sustained availability still need evaluation. Earlier frontend checks covered opening/closing, ordinary product search, an excluded account page and a390px viewport without horizontal overflow. The October22 pilot additionally exercised two real website chat requests.

The local readiness endpoint returned ready on October 21. This is not acceptance of public deployment. See [fix log](FIXLOG.md).

Release preparation on October 22 reran 70 formal offline checks: 14 answer-gate, 19 product/internal-evidence, 6 local-page/timeout, 9 API-entry, 10 reranker-window and 12 intent-review/service checks. Dependencies were simulated where appropriate; no paid model requests were made for these offline checks. After separate activation approval, the1.2.0 widget and exact-path tunnel ingress were deployed. The two authorized website questions used the live model; see [release status](RELEASE.md).

## Deployment and Data

The full request path has been exercised locally and on the real website. The fixed HTTPS tunnel exposes health endpoints and the exact session/chat paths; demo and OpenAPI remain blocked. A public request without a session receives401, while CORS preflight succeeds for the shop origin.
The widget is installed on tayunetea.com with chatEnabled=true. One budget-product query and one opened-tea return-policy query completed with verified sources and without degradation. This does not establish full catalog recall, general factual accuracy or comprehensive safety. The connector runs on the local computer, without an installed startup service or an availability guarantee.
Model weights, private source documents, generated datasets, logs and session databases are excluded.
No production availability, answer accuracy or commercial impact is claimed.

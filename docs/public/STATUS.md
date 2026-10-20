# Development Status

Updated: 2026-10-20.

This is a development baseline, not a production release.

## Implemented

- Document processing, BGE-M3 embeddings and isolated Milvus storage.
- Dense/sparse retrieval and a reranking integration.
- Query classifier inference and conversation summary integration.
- LangGraph model/tool feedback loop with bounded attempts.
- WooCommerce read-only product tools.
- SQLite sessions, request budgets and FastAPI endpoints.
- Local browser demo and a WordPress search integration template.

## Known Acceptance Failures

- Brewing retrieval can fail when a candidate exceeds the reranker's 512-token limit.
- Public policy lookup failed during a user acceptance run; the original failure cause remains unresolved.
- Product search can include non-tea items, and displayed sources can include candidates not used in the final recommendation.
- Budget filtering in the current product tool is not fully wired and contains field-name errors.
- Tool failures need better local diagnostics, and unnecessary repeated lookups can exhaust the attempt budget.

These issues are recorded for subsequent fixes and regression checks. They have not been declared resolved.

## Deployment and Data

The full request path has been exercised locally. Public deployment and website acceptance are pending.
Model weights, private source documents, generated datasets, logs and session databases are excluded.
No production availability, answer accuracy or commercial impact is claimed.

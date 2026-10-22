# Public Pilot Release

Updated: 2026-10-22. Status: prepared, not publicly activated.

The deployed site still uses widget1.1.2 with chat disabled. The repository contains release candidate1.2.0 with chat enabled; uploading it and opening the tunnel API paths remain separate deployment actions.

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

## Prepared Verification

70 formal offline checks passed: answer gate14, product/internal evidence19, local widget/timeout6, API entry9, reranker windows10, intent review/service12. These do not load production models or measure real-answer accuracy. PHP syntax and the production frontend build passed.

Three authorized local widget cases passed after the evidence fix; details and limits are recorded in[EVIDENCE_FIX.md](EVIDENCE_FIX.md). Public-site question acceptance is pending and must not be inferred from localhost tests.

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

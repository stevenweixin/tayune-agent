# Answer Evidence and Product Content Fix

Date: 2026-10-22

## Observed Bugs

- A contextual brewing follow-up could return a specific recipe without using a tool or citing evidence from the current request.
- Approved knowledge chunks backed by a local document had no public URL. The source registrar skipped them, leaving otherwise traceable tool results without usable citation IDs.
- Product detail normalization omitted the description, including the brewing instructions already available through the public product API.
- Candidate source links could be displayed without being cited in the final answer. Product search also needed explicit catalog scope and listed-USD-budget constraints.

## Changes

- Added a final-answer citation gate. Unknown source IDs and uncited free-form answers are rejected, with at most one correction cycle within the existing model/tool budgets. Source-free greetings and clarification replies use fixed text.
- Register public evidence by URL and internal evidence by retrieved chunk identity. Internal references show a document title and an explicit no-public-link note; local file paths are not exposed as source links.
- Extract bounded plain text from product descriptions only when fetching a product's details. Catalog listings remain compact. Missing text stays empty, and truncated text is explicitly flagged.
- Updated tool instructions to inspect the selected product's own description before substituting a general brewing guide. Product IDs must still come from known search results.
- Preserve product-category scope checks and decimal USD filtering. Filtering applies only to the returned page; it does not establish complete catalog coverage or include checkout shipping and tax.
- Display only public sources referenced by the answer, rather than every retrieved candidate.

The changes are limited to evidence handling and the paired product/knowledge tool interfaces. They do not enable public chat, modify database content, or add a new model or service.

## Verification

33 offline regression checks passed against the formal modules, using mocked model, HTTP and knowledge dependencies:

```bash
python -B -m unittest discover -s tests/unit -p test_answer_guard.py -v
python -B -m unittest discover -s tests/unit -p test_product_evidence.py -v
```

Coverage includes missing/unknown citations, bounded correction and tool attempts, per-request state, internal/public evidence, product HTML extraction and truncation, missing descriptions, compact listings, product-ID restrictions, redirect rejection and cancellation propagation.

The same 33 checks also passed in an isolated snapshot containing the repository baseline plus only this change's files. This verifies that the regression tests do not depend on unrelated uncommitted changes or local reference modules.

A preceding read-only public API check confirmed that the selected product's description contained its mug-brewing recipe and that the extraction path retained it. That check was not an Agent answer evaluation.

At the original fix commit, offline checks were complete but the final changes had not yet been retested with the live model. The subsequent authorized retest is recorded below.

## Controlled Live Retest

Three separately authorized questions were sent through the production widget on the loopback-only test page, in one new conversation. Testing stopped after the third question. Public chat was not enabled.

| Case | Observed result | Actual successful tool calls |
| --- | --- | --- |
| Oolong below USD50, excluding teaware | Returned Dong Ding 500g at USD45.90, in stock, with its product source; inspected product page matched these details. Complete catalog recall was not assessed. | Three product searches |
| Follow-up: how to brew that tea in a mug | Resolved the prior product, retrieved its details, and answered approximately 3g per 250ml for 2–3 minutes. The current product page supports that recipe. The answer distinguished the separate gongfu recipe and did not invent a mug temperature. | One product search and one detail lookup |
| Ripe pu-erh in a 100ml gaiwan, using the internal guide if available | Returned the guide's 5–6g, 95–100°C and initial pour at 10–15 seconds, with an internal-reference title and no fabricated link or exposed local path. Checked against the approved guide. | One knowledge lookup |

All three completed without degradation. Classifier-to-completion log intervals were approximately 6.64s, 5.11s and 9.28s respectively; these are not precise browser end-to-end timings or performance benchmarks. No browser warnings/errors were captured during this retest.

An independent, read-only direct product-API check failed once with an unavailable-service result. The actual Agent detail lookup succeeded, and the current public product page was inspected for verification. These smoke cases do not establish sustained service availability.

The request summary's existing `tools` value counts trace entries, including classification and answer validation. The actual tool counts above use the individual tool-stage records, not that summary number.

## Remaining Limits

- Valid citation IDs prove reference membership, not that every statement or recipe number is supported by the cited text.
- An internal guide for one tea cannot establish another tea's product-specific recipe.
- Long descriptions may be truncated; unseen instructions remain unknown.
- The targeted product/mug/internal-guide cases passed, but broader business, safety, failure and public-deployment cases still require controlled validation before release.
- Answers remain verbose, and the mug answer unnecessarily displayed a public product ID. The internal-guide answer could more clearly distinguish gaiwan capacity from actual water volume; these checks are not a general language-quality assessment.

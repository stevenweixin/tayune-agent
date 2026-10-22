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

The earlier live retest confirmed that the citation gate could safely degrade a brewing answer, but the final product-description/internal-reference changes have not yet been retested with the live model. No additional paid model requests were made for this verification.

## Remaining Limits

- Valid citation IDs prove reference membership, not that every statement or recipe number is supported by the cited text.
- An internal guide for one tea cannot establish another tea's product-specific recipe.
- Long descriptions may be truncated; unseen instructions remain unknown.
- Real-model tool selection, recipe grounding and broader business cases still require controlled validation before public release.

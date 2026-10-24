# WordPress 1.4.0 Preflight

Date: 2026-10-24. Status: **packaged locally, not uploaded**.
The public homepage still returned widget1.3.1 during the read-only asset check.
No WordPress, tunnel, quota, backend-runtime or Milvus operation was performed.
No real session/chat request or paid model call was made in this preflight.

## Changes and Contracts

- Preserve the approved orb, right-hand placement, frosted panel, inline title links and privacy text.
- Reuse a request ID only for a manual retry of the same question in the same session after an interrupted request. A completed question, changed text, new session or stale409 response gets a new ID. No automatic resubmission.
- Start a nine-second frontend interval when dispatching chat, allowing margin around the backend's existing eight-second start-to-start gate. Preserve the draft and disable early sends, including Enter. This is UX, not a security boundary; backend enforcement remains unchanged.
- A429 wait never shortens an existing deadline. The countdown checks the deadline repeatedly, even when an extension leaves the rounded displayed number unchanged; the earlier one-shot timer could stall in that case.
- Require a valid completion object and nonempty JSON answer parts. Malformed200 responses produce an error and keep the question for manual retry, rather than silently appearing successful. Streamed text is not appended again as a second JSON answer.
- Session timeout remains10s. Chat wait is115s:85s backend processing, approximately20s nominal delivery for the12000-character extractive limit, plus10s margin. This is bounded waiting, not a delivery guarantee under arbitrary network delay.
- Preserve the public session/chat paths and credentials-omitted requests; no management endpoint or key is exposed by the plugin.

## Verification

162 Python checks and8 Node checks passed. Ten Python additions cover packaging and the PHP entrypoint; the new Node check verifies the stream-time budget calculation. The existing formal regressions use temporary or mocked dependencies where appropriate, not live production data.

The production bundle was also loaded against a loopback-only fixture, never a proxy to the real API:

| Fixture | Observation |
| --- | --- |
| Stream | Text/link visible before completion; new-conversation control stays disabled while active. |
| JSON | One complete answer; unsafe URL/HTML-like text remained literal, with no injected image element. An immediate second question remained a draft without another POST. |
| Interrupted stream | Partial answer marked incomplete, draft restored, no automatic retry. Two manual attempts used the same request ID and one simulated execution key. This is not a provider billing guarantee. |
| Expired401 | Explicit notice; manual resubmission created a new fixture session and request ID. |
| Stale409 | Explicit resubmit notice; the next manual submission used a new ID in the same session. |
| Limited429 | Draft retained, send disabled, no automatic resubmission. |
| Extended429 | Retry-After9 extended the deadline without changing the initial rounded9; countdown subsequently unlocked, rather than freezing. |
| Malformed200 | Error shown, question retained, no blank successful assistant answer. |
| Disabled chat | Input disabled, no fixture API requests; ordinary page search submitted independently. |

The timer correction was followed by targeted extended-wait and mobile-stream checks on the rebuilt bundle. PHP stubs verified matching resource version, public-only configuration and exclusion of account/checkout pages; these are not live WordPress tests.

Browser dimensions checked:1280x900,393x852 and320x640. The panel stayed inside each viewport, with no horizontal page overflow. Pause held the frame counter stable, resume advanced it, Escape returned focus to the launcher, and New Conversation cleared the visible history before creating a new fixture session on the next explicit send. No browser console errors were captured. Screenshots and fixture-only request records are retained under ignored reports/.

These checks do not reproduce iOS Safari, its keyboard/address-bar changes, a115-second real timeout, the production theme/cache combination or a real public SSE request. Original iPhone confirmation and final website acceptance remain pending.

## Package

Run from the repository root with the existing frontend dependencies and PHP CLI:

```bash
python -m scripts.package_wordpress
```

The script builds production assets, lints PHP, compares versions, packages an explicit allowlist and checks ZIP integrity. Existing release filenames are not overwritten.

Artifact: `reports/releases/tayune-tea-companion-1.4.0.zip` (74495 bytes).

SHA256: `521287f5877d0d43ed7403bfe44613a32728789a7fd30b1b2ffa0cb420154e1c`.

Only these files are included, beneath one top-level plugin folder:

```text
tayune-tea-companion/
  tayune_assistant.php
  assets/tea_companion.js
  assets/tea_companion.js.LEGAL.txt
```

ZIP contents were compared byte-for-byte with the current built files. No source references, node_modules, credentials, environment files, knowledge data, models or databases are included. Earlier local intermediate packages are retained under distinct names and are not the release artifact.

## Next Approval

Upload/update requires a separate operator confirmation. Keep the previous1.3.1 archive for frontend rollback; this does not roll back the backend or database. After deployment, check the public resource version, desktop/mobile interaction and ordinary shopping/search paths. Any real-model questions require explicit count/cost authorization and must respect cooldowns; no automatic retry or quota changes are permitted.

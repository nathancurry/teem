# Vertical slice 3: phone dictation and notification hints

Status: implemented increment after [slice 1](vertical-slice-1.md) and [slice 2](vertical-slice-2.md). Its phone interface and Web Push are superseded by [slice 4](vertical-slice-4.md); the transcription runner is reused.

## Outcome and scope

Deliver two paths:

```text
Installed phone PWA → record objective / acceptance criteria
  → local transcription → editable text → ordinary Teem proposal
  → deliberate UI approval → existing workflow → Ready to merge

Meaningful workflow transition → generic push hint
  → phone opens Teem → authenticated fetch of current durable state
```

Keep the existing server, PostgreSQL, artifact store, outbound worker, deterministic proposal module, browser authentication, and decision controls. Add microphone input to the existing fields, concise mobile status, and a small Web Push outbox. Worker, Candidate, Review, lease, reconciliation, authorization, and evidence semantics remain unchanged.

**Defer TTS.** Dictation, visual responses, and notifications provide the first useful phone experience. Spoken output and voice Approve/Deny controls do not belong in this slice.

## Phone PWA and authentication

Extend the existing manifest with a stable identity, standalone display, and suitable phone icons. Add a small browser script and a root-scoped service worker for push display and notification navigation. Serve the manifest, icons, script, and service worker without authentication; these static files contain no project data. Authenticated pages and API responses remain `no-store`. Offline submission, cached workflow state, and background sync are deferred.

Retain browser Basic authentication over HTTPS and the existing exact configured `Origin` check on browser POSTs. Apply both checks to audio uploads and subscription registration/removal. Use ordinary same-origin authenticated requests; do not store credentials in JavaScript or give them to the service worker. The worker retains its separate bearer credential and protocol version 2. No session table, custom login, CSRF-token migration, or authentication redesign is required by this design.

Verify Basic authentication in the installed iPhone and Android apps early, including authenticated uploads and opening a notification after the app has closed. If a target device demonstrates a concrete authentication incompatibility, record it and choose the smallest necessary compatibility fix before proceeding with that target. Do not assume a redesign is necessary. If authentication is needed, open the protected page through the existing browser challenge and fetch current state; never automatically replay a mutation.

The mobile home view shows pending approvals first, then active and recent Runs. Show project/objective, current phase, revision round, last refresh time, stop reason when present, and a concise check/review summary with links to existing evidence. Use server-derived labels. Distinguish unresolved execution from failure and confirmed stop; Ready to merge is not merged.

Poll current status every five seconds while visible and refresh on focus, reconnect, and notification navigation. On fetch failure, label the display as last seen and require a refresh before acting. Simple page handlers are sufficient; no browser lifecycle abstraction or new realtime transport.

Pending decisions are the existing `awaiting_approval` Runs and scoped `code_check_review` approval forms. Show the exact objective, criteria, repository/base, actions, checks, reviewer configuration, and limits before the existing Approve/Deny buttons. Keep the version check, Run lock, Approval record, and controller transitions. A blocked Run does not acquire an approval-to-resume action. Cancellation and retry retain their existing controls and eligibility.

## Dictation lifecycle

1. Select a registered project and the **Objective** or **Acceptance criteria** field. Each field has a microphone control; normal typing remains available. Do not infer project, criteria, or permissions from a monologue.
2. A deliberate push starts capture with a visible recording indicator; release stops. Provide an accessible tap-to-start/tap-to-stop equivalent. Request microphone permission only on this interaction. Stop all media tracks on cancel, interruption, page hide, or navigation, including a late permission grant after cancellation. Never automatically resume recording.
3. A normal stop uploads the bounded clip to authenticated `POST /transcribe`. Explain before capture that the clip goes to this Teem server for local recognition. Show a transcription-in-progress indicator.
4. Put the returned text into the selected editable field for review. A cancelled or superseded response must not overwrite another recording or subsequent edits. Recognition is untrusted input: show the actual text, including potentially incorrect paths, numbers, and negations. Do not silently rewrite intent.
5. The user inspects both fields and taps the existing **Propose** control. Submit through `/requests` with the existing project, objective, criteria, revision limit, and deduplication key. Use the existing validation and `proposal` classification. Recording and transcription alone create no Request or authorization.
6. Show the ordinary persisted proposal and contract. The user approves or denies through the existing authenticated UI. Only the existing controller transition records authorization and queues work. Saying “yes” or “approve” in a transcript has no decision semantics.

Keep existing Request deduplication and decision conflict behavior. Reuse the same key when explicitly retrying the same form after an uncertain response; use a new form/key for a new proposal. If a proposal or decision acknowledgement is lost, fetch current Run/recent Request state before acting again. A repeated decision may return the existing stale/conflict response; refresh instead of adding a new decision-replay protocol. No offline mutation queue or durable draft recovery.

Permission denial, unsupported media, empty/invalid output, timeout, or lost upload produces an input error with typing or a fresh recording available. Never convert a transcription failure into a Run failure or automatically submit partial input.

## One bounded local runner

Choose one concrete server-invoked `whisper.cpp` runner. Browser recognition has varying support and may route audio through a browser vendor's service, as described by [MDN](https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition). A local subprocess gives Teem an explicit data destination without embedding inference in the controller or introducing a provider interface.

Install FFmpeg, `whisper-cli`, and one model outside repository control. Operator configuration fixes executable/model paths, model identity, and language. FFmpeg converts tested phone recordings to mono 16 kHz, 16-bit WAV; `whisper-cli` transcribes them, following its [documented CLI input format](https://github.com/ggml-org/whisper.cpp#quick-start). Return bounded UTF-8 text as `{"text":"..."}`. No hosted fallback, runtime model download, translation, or transcription-provider abstraction.

Use ordinary HTTP upload and a synchronous subprocess invocation. Admit one transcription at a time; return `429` while busy. Do not hold database transactions during upload/inference or delay worker requests and controller recovery. Transcription creates no Task, Attempt, durable job, or workflow event.

| Limit | Initial bound |
| --- | --- |
| Clip | 60 seconds; visible stop at the limit, with server validation of decoded duration |
| Upload | 8 MiB; 30-second upload timeout |
| Decode and inference | 90 seconds combined; terminate the process group on timeout |
| Output | 8,000 characters and 32 KiB UTF-8; reject overflow |
| Resources | One invocation, 2 GiB memory; use finite subprocess/resource limits appropriate to the host |

Negotiate a supported `MediaRecorder` format and initially accept tested WebM/Opus and MP4/AAC. Validate media rather than trusting MIME or duration claims; reject malformed or overlong input instead of silently truncating. Invoke fixed argument vectors without a shell. Use the existing Bubblewrap approach to restrict the runner to its binaries/model/input and private scratch, with no network, repository/artifact access, or server credentials. Stop child processes on server exit. Details of installation and resource enforcement are implementation work, not a new execution subsystem.

## Exact durable additions

Add only two tables. All columns are required unless marked nullable; timestamps are `timestamptz`.

| Table | Columns and constraints |
| --- | --- |
| `push_subscriptions` | `id uuid PRIMARY KEY`, `endpoint text UNIQUE`, `p256dh text`, `auth text`, `created_at DEFAULT now()` |
| `notification_deliveries` | `event_id bigint REFERENCES events(id)`, `subscription_id uuid REFERENCES push_subscriptions(id) ON DELETE CASCADE`, composite primary key `(event_id, subscription_id)`; `state text` constrained to `pending`, `accepted`, `abandoned`; `attempt_count integer DEFAULT 0` constrained to 0–6; `next_attempt_at DEFAULT now()`; nullable `completed_at`. Index pending rows by `next_attempt_at`. |

Reuse the existing event committed with each qualifying workflow transition as the delivery's source; it supplies the Run and event time. No extra notification event kind or state snapshot is needed. In that same transaction, insert one pending delivery per currently registered subscription. Insert once for the final qualifying transition, including sweep, reconciliation, and detected artifact-integrity failures. Preserve the existing Run → Task → Attempt lock order. Report replay, polling, or transaction rollback must not produce additional deliveries.

No changes to workflow tables, statuses, approval scope, worker protocol, or Request classification. No durable audio, transcript, session, pending-decision, or notification-preference records. Use explicit SQL to add these tables during implementation; existing initialization is not a migration tool. Do not backfill historical notifications.

## Minimal Web Push

| Qualifying committed transition | Reason to notify |
| --- | --- |
| Proposal created in `awaiting_approval` | Decision required |
| Entry into `ready_to_merge` | Candidate ready to inspect |
| Entry into `blocked`, `failed`, `checks_failed`, or `uncertain` | Attention required |

Notify on the committed Run outcome, not every failed Attempt or lease renewal. Normal coding/review/revision progress, denial, and user cancellation need no push. Readiness revoked by an integrity failure qualifies as blocked. Notifications never schedule, authorize, retry, or complete work.

Use one standard Web Push library for encryption/VAPID and one stable server-controlled VAPID key. An explicit **Enable notifications** interaction requests permission and registers the browser subscription through authenticated HTTP. Display enabled/unavailable status and allow disabling it. Repeated registration for the same endpoint updates keys without creating duplicates. Keep only the subscription UUID locally to remove/replace that install's server record; it is not a credential. Registration applies to future transitions; the dashboard supplies current state.

The iPhone path is an installed Home Screen web app with an explicit permission gesture; verify target support as part of the device check. See [WebKit's Web Push requirements](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/). Permission denial or unavailable push leaves the foreground workflow usable.

The encrypted payload contains only a Run UUID as a navigation hint. The service worker displays generic text such as “Teem has an update. Open Teem to view it,” using a per-Run notification tag to collapse repeats. Include no project names, transcript, findings, status claim, credentials, or approval actions. The service worker needs no authenticated background fetch. Clicking opens a validated same-origin Run URL and fetches current state through existing authentication. Delayed, duplicated, or reordered hints may refer to resolved conditions; they remain harmless because the display comes from current durable state. No stale-message classification machinery is needed.

Run one sender loop in the existing server, separate from the controller recovery sweep. Select due pending rows in short transactions. Before the HTTP call, durably increment the attempt count and set the next retry time; release database locks before network I/O. Use a ten-second send timeout. On restart, resume pending rows; no separate service, delivery lease, or broker.

- Push-service acceptance marks the row `accepted`, with `completed_at`; it does not prove phone delivery or reading.
- Retry transport failures, `429`, and `5xx` at most six total attempts, after 30 seconds, two minutes, ten minutes, one hour, and six hours. Respect a longer `Retry-After` within the expiry bound.
- Expire delivery 24 hours after its source event. Set push TTL to the remaining lifetime. Exhaustion, expiry, and other permanent errors mark the row `abandoned`, with `completed_at`.
- `404`/`410` removes the expired subscription and its delivery rows. Other errors do not remove unrelated subscriptions.
- A crash after sending but before recording acceptance can duplicate a hint; a crash after reserving an attempt may consume it without sending. These bounded best-effort semantics are sufficient. Workflow correctness never depends on delivery success.

If no subscription exists, enqueue nothing. Missed or expired pushes remain discoverable through current Run status and pending approvals. Ordinary sanitized error logging is sufficient; no notification-health product, read receipts, reminders, or preference framework.

## Retention and privacy

Disclose that **local recognition runs on the Teem server, not the phone**. The HTTPS terminator, server host, and local runner receive the audio; no external speech service receives it.

| Data | Destination and retention |
| --- | --- |
| Raw phone recording | Page memory through upload; release on success/failure, cancel, or page hide/exit. Retrying failed transcription requires a fresh recording. Never save it in browser storage or a service-worker cache. |
| Server audio and temporary transcript | Private temporary input/scratch for the bounded operation; delete on completion/error and clean abandoned files before enabling transcription after restart. No audio artifact, database row, content log, or archive. A stopped server performs cleanup on its next start. |
| Editable draft text | Phone page memory until submission, discard, or page exit. No persisted draft or transcript history. |
| Submitted text and derived workflow text | Existing Request/contract/evidence retention: the Run's lifetime, currently indefinite. Existing authorized coding/review context may include this text and follows the project's current routing policy. Local recognition grants no new downstream permission. |
| Push data | Push service receives encrypted navigation data and transport metadata; the OS shows generic text. Teem's delivery window is 24 hours; provider/OS retention and already displayed notifications are outside Teem's control. |
| Notification records | Keep subscriptions until disabled, replaced, or rejected as expired. Delete accepted/abandoned delivery rows seven days after `completed_at`; existing workflow events retain their normal history. |

Implementation requirements are limited to HTTPS, bounded private scratch, cleanup on all operation exits/restart, and keeping audio/draft content and credentials out of logs, caches, and backups. Validate browser subscription keys and restrict delivery to the tested browsers' HTTPS push-service destinations; reject redirects and local/private targets. Use a small concrete validation rule, not a generalized destination-policy system. Swap, crash-dump settings, backup tooling, and broader operational hardening follow deployment practice and are not new subsystems in this slice. Application deletion is not a secure-erasure guarantee.

## Acceptance

Retain slice-1/2 regression coverage. Add three focused acceptance scenarios using real PostgreSQL/HTTP and a controllable push endpoint; reuse existing workflow fixtures.

1. **Phone input through authorization:** Record objective/criteria, transcribe with the actual local runner, edit text, submit an ordinary proposal, and approve with existing controls. Reach Ready to merge through the existing workflow. Verify transcription alone, including text saying “approve,” never creates authorization; unauthenticated and cross-origin new endpoints are rejected. Keep existing deduplication and stale-decision behavior.
2. **Audio failure/privacy:** Cover the two phone recording formats, bounded malformed/overlong input, runner timeout, and recording interruption. Confirm track/process cleanup, no durable audio/draft, and text fallback. Restart during transcription: no Request is created and temporary files are cleaned. Speech failure must not interfere with worker/controller progress.
3. **Notification durability:** Commit a qualifying transition, restart before sending, and recover delivery. Exercise rollback, replay, lost send acknowledgement, transient retry, expiry, and dead subscription removal without duplicate outbox rows or workflow effects. Cover decision-required, readiness, and attention states, including uncertainty. Open an old hint after state changes and verify authenticated current state. With push denied or unavailable, pending decisions remain visible on foreground refresh.

Keep a manual check on **one real iPhone Home Screen installation and one real Android installed PWA**: install/relaunch, Basic authentication and authenticated upload, actual recording format and track shutdown, background/closed-app push, notification click, current-state refresh, and ordinary Approve/Deny controls. Record device/browser versions. Emulation does not establish these OS behaviors; unsupported capabilities must be recorded as fallbacks, not passing phone acceptance.

## Remaining choices and deferrals

Before acceptance, pin the local runner/model and Web Push library, confirm model latency fits the server and bounds, record working device/browser/codec combinations, and configure VAPID and the supported push destinations. Run the device authentication check first; authentication redesign remains deferred unless a demonstrated incompatibility requires a narrow fix.

Defer voice Approve/Deny, TTS, ambient/always-listening capture, wake words, Omi, AirPods-specific behavior, native apps, continuous audio, WebRTC, browser/hosted speech providers, provider abstractions, and conversational intent extraction. Also defer offline drafts/submission, transcript history, session/auth redesign, generic notification/event frameworks, preferences, reminders, lifecycle abstractions, and operations-platform work.

Keep the slice-2 deferrals, including autonomous merge/deployment, expanded permissions, contract mutation, parallel work, and additional users/workers. No Redis, NATS, service mesh, or vector database.

# Vertical slice 2: independent review and bounded revision

Status: implemented increment after [vertical slice 1](vertical-slice-1.md). Deliver a preserved Candidate with applicable passing checks and an independent passing Review, displayed as **Ready to merge**.

## Scope

Keep the existing server, PostgreSQL, artifact directory, outbound worker, and text PWA. The manager remains a deterministic proposal/status module. Add one concrete configurable reviewer runner, one Task kind (`review`), and one evidence table (`reviews`). Reuse `code_and_check` for initial coding and revisions; checks remain part of that Task.

The serial path is:

```text
Candidate + passing checks → fresh review
  pass             → Ready to merge
  blocked          → stop
  changes_required → revision → new Candidate + rerun checks → fresh review
```

Allow at most two revision Tasks and two Attempts per Task, subject to a Run-wide Attempt cap and absolute deadline. A valid judgment is never retried in search of a different verdict. Failed objective checks stop the Run; automatic test-failure repair is deferred. Only controller transactions schedule work or declare readiness.

## Authorization and concrete limits

The proposal explicitly covers coding, checks, independent read-only review, and bounded revisions toward the original acceptance criteria. Record the authenticated decision in the existing Approval table with action `code_check_review`. Dispatch requires that action and matching worker-local configuration. Findings cannot expand authorization; merge and publication are excluded.

Keep the contract fixed throughout the Run. Retain the existing objective, acceptance criteria, project, base, context version, checks, and check hash. Add allowed actions `review` and `revise`, the Ready to merge delivery condition, a revision limit, and the selected reviewer configuration identity. Record the concrete runner identity, review instructions hash, and authorized data destination with the proposal; these are fixed values for this runner, not generalized policy schemas. Executables and host paths come from worker-local configuration, never from the server or model output.

Enforce only these execution limits:

| Limit | Slice-2 rule |
| --- | --- |
| Attempts | Default eight across the entire Run, including both roles, retries, and revisions; at most two per Task. Checks inside a coding Attempt do not create additional Attempts. |
| Revisions | Configured integer 0–2; creating a revision Task consumes a round even if it fails. |
| Subprocess timeout | Retain the 15-minute coder and five-minute per-check limits; configure one finite reviewer timeout. Clamp every subprocess to the remaining Run deadline. |
| Absolute Run deadline | Retain the 30-minute allowance from approval. Never reset it on retry, revision, reconnect, or restart. |

Count Attempts and revision Tasks under the Run lock before dispatch/queueing. An admitted Attempt can finish when the Attempt count equals its cap; the cap prevents another Attempt. A final review can pass at the revision cap. The deadline must still be unexpired at completion. These limits do not promise that every allowed revision will fit.

Retain existing OS resource and artifact/output bounds. Use a finite review input cap and bounded output. Do not claim tool-use or monetary limits: provider cost accounting, micro-USD pricing, generalized tool-call metering, and reservation/settlement machinery are deferred. Existing `attempts.usage` may retain execution diagnostics; it is not a ledger.

A changed objective, base, check configuration, reviewer configuration, permission, or limit requires a new proposal and Run. Historical slice-1 approvals do not authorize this workflow; continuing those Runs is deferred.

## Durable state

Keep Candidate bundles and immutable check evidence in the existing artifact store and `candidates` table. Add only the following state:

| Table | Change |
| --- | --- |
| `runs` | `current_candidate_id` referencing `candidates`; nullable `stop_reason`. Add statuses `reviewing`, `ready_to_merge`, and `blocked`. |
| `tasks` | Permit `code_and_check` and `review`. Add `revision_number` (0 for initial coding and its review), nullable `input_candidate_id`, nullable `source_review_attempt_id`, and nullable `review_context` JSON. Unique `(run_id, revision_number, kind)`. |
| `attempts` | Nullable `result_sha256` for the canonical supervisor final report, supporting identical replay and conflicting-report rejection. |
| `reviews` | New immutable evidence table described below. |

A Task uses its Run's frozen contract version. Initial coding has no input Candidate. Revision coding references its parent Candidate and the accepted `changes_required` Review that caused it. Review references the Candidate from coding in the same round and stores its immutable context pack. Retries keep the same Task inputs. Validate same-Run/contract links and Task kinds under the Run lock. Candidate ancestry follows its producing Attempt and Task; no extra lineage table is needed.

The `reviews` table contains:

| Column | Meaning |
| --- | --- |
| `attempt_id` | Primary key and foreign key to the review Attempt; also the Review identity. |
| `raw_output` | Bounded exact reviewer output; overflow retains a prefix and an explicit validation error. |
| `result` | Validated JSON judgment, or NULL for invalid output. |
| `validation_error` | Nullable validation/execution diagnostic. |
| `provenance` | Supervisor-observed runner/model identity, instructions hash, context hash, invocation start/end times. |
| `disposition` | `accepted`, `invalid`, or `stale`, fixed by the controller on insertion. |
| `created_at` | Receipt timestamp. |

The immutable Task supplies Candidate and contract identity. At most one accepted Review exists per Task. Any accepted verdict finishes the review Task successfully; its verdict determines the Run transition. Invalid output fails the Attempt. Stale evidence cannot advance the Run. Preserve all prior Candidates, Reviews, contexts, and final-report digests; never overwrite them to reflect a later judgment.

No other tables are required. Assignment/result JSON extends the existing worker journal without a new journal schema. Bump the protocol version and reject incompatible workers.

For the development database, use fresh initialization when no data must be retained. If data must be retained, inspect it and apply only the explicit SQL alterations/backfills it requires; `CREATE TABLE IF NOT EXISTS` alone cannot update existing tables. Stop assignment and reconcile outstanding executions before changing the schema. Do not build a migration framework, historical Run continuation, or speculative compatibility states. If retained slice-1 rows conflict with the new active-project index, resolve those actual rows during the development upgrade without granting review authority or marking them Ready to merge.

## Exact Candidate and fresh review

After passing checks, the controller prepares a bounded context pack from the server's verified Candidate bundle, never the coder's workspace. Include:

- Run/Candidate IDs, frozen contract version and hash, objective and acceptance criteria;
- original base/head, bundle hash, check configuration/hash and exact preserved check evidence;
- review instructions and configured runner identity;
- base-to-head diff and tracked textual source at the Candidate head, including base content for deleted files.

Serialize deterministically and hash the complete pack. Repository-relative source references identify paths and line ranges; check references identify configured check names. Include identities and explicit omissions for nontext files. Missing necessary source, unsupported changed binary/submodule content, or input-cap overflow blocks the Run with `review_input_unavailable`. Do not silently truncate context and accept a pass. Larger-repository retrieval is deferred.

Build the pack outside a long transaction, then recheck the Run, current Candidate, contract, cancellation, and deadline under lock before storing it and queueing review. `awaiting_review` is the durable recovery point if preparation is interrupted. The unique Task key makes recovery repeatable.

Every invocation, including retries and later rounds, starts in a fresh process/session. Give it no coder conversation, scratchpad, self-assessment, prior verdict, or revision explanation. The coder and reviewer may use the same model; independence means separate context and permissions. Source and check output remain untrusted evidence and may contain misleading instructions.

Use one worker-installed configurable runner outside repository control. It reads the pack, returns bounded JSON, and has no repository command tools, writable Candidate, coder workspace/cache, worker credentials, or publish credentials. Supply read-only inputs and a fresh disposable scratch directory. The server also rejects Candidate uploads from review Attempts.

This design does not select local or remote inference. The chosen runner must preserve fresh context, read-only access, timeout/cancellation enforcement, and the approved data destination. Any inference access is limited to that configured destination and its required credentials; it grants no general network or host access. Keep existing coding/check isolation. No provider abstraction or product-quality local-model selection is part of this slice.

## Review result

Require one strict, bounded JSON object:

```json
{
  "candidate_id": "<assigned UUID>",
  "contract_version": 1,
  "context_sha256": "<assigned digest>",
  "verdict": "changes_required",
  "summary": "The empty-input criterion is not met.",
  "findings": [
    {
      "criterion": "<original criterion or objective reference>",
      "description": "<observed issue and required outcome>",
      "evidence": [
        {"kind": "source", "path": "src/example.py", "start_line": 12, "end_line": 16}
      ]
    }
  ],
  "uncertainties": []
}
```

All keys are required; reject unknown keys, wrong types, and assignment identity mismatches. A finding needs nonempty criterion/description text and at least one source reference or `{ "kind": "check", "name": "<configured name>" }`. Validate reference existence and line bounds against the pack. No finding IDs, severity taxonomy, adjudicator, or findings table are needed.

- `pass`: nonempty summary, no findings or uncertainties.
- `changes_required`: nonempty summary, at least one finding, no uncertainties.
- `blocked`: nonempty summary and at least one nonempty uncertainty; findings are optional.

The controller validates shape and applicability, not the truth of a model's interpretation. Preserve malformed/oversized output as invalid evidence; never repair it with another model or convert execution failure into a judgment. Reject an unauthenticated or unidentifiable transport envelope at the protocol boundary.

## Revision and controller transitions

A revision receives the original contract, parent Candidate, accepted findings, and check evidence. Recover the parent from its server bundle into a new writable sandbox and start at its head. Freeze a changed source tree into a new commit/bundle descending from both the parent head and original base. An empty commit is not a revision. Run every configured check again in separate disposable checkouts of the new head and preserve a new Candidate, including failed-check evidence. A failed Attempt before preservation leaves the parent current; a retry starts from that same frozen parent.

All transitions lock Run → Task → Attempt, use stable ID ordering for multirow scans, and commit state plus append-only events atomically. At most one queued, running, or unresolved Task exists per Run. Queue insertion and the transition causing it are atomic, except for the explicitly recoverable context preparation above.

| Input | Controller action |
| --- | --- |
| Scoped approval | Queue initial `code_and_check`, round 0; Run → `queued`. |
| Coding claim | Create Attempt within limits; Run → `coding`. UI derives “Revising” from the Task round. |
| Preserved Candidate with complete check evidence | Set current Candidate and finish coding Task. Passing checks → `awaiting_review`; failures → `checks_failed`. |
| Prepared context for the current passing Candidate | Store pack and queue its unique review Task; remain `awaiting_review`. |
| Review claim | Create Attempt within limits; Run → `reviewing`. |
| Applicable `pass` | Insert accepted Review, finish Task/Attempt, validate completion predicate, Run → `ready_to_merge` in one transaction. |
| Applicable `changes_required` | Insert accepted Review and finish Task/Attempt. If limits allow, queue the next revision referencing this Candidate/Review; Run → `queued`. Otherwise → `blocked` with the exhausted limit. |
| Applicable `blocked` | Preserve Review and finish Task/Attempt; Run → `blocked`, reason `review_uncertain`. No automatic re-review or revision. |
| Known execution failure or invalid review | Finish failed Attempt. If eligible, requeue the same Task once (`queued` for coding, `awaiting_review` for review); otherwise → `failed`, or `blocked` if a Run limit prevents progress. |
| Next work would exceed a Run limit | Queue/claim nothing; → `blocked` with the exhausted limit after execution is settled. |
| Lease loss or unknown execution | → `uncertain`, or retain `cancelling`; no replacement until reconciliation confirms stop. |
| Cancellation or deadline | Stop dispatch and request active execution stop. After stop confirmation, cancellation → `cancelled`; deadline → `blocked` with reason `deadline`. Queued work can stop immediately. |

Use ordinary Task statuses (`queued`, `running`, `succeeded`, `failed`, `uncertain`, `cancelling`, `cancelled`); review phase belongs to the Run. Add a review-result Attempt outcome. Cancellation changes pending Tasks only, preserving finished history.

The active-project index must cover `awaiting_approval`, `queued`, `coding`, `awaiting_review`, `reviewing`, `uncertain`, and `cancelling`. Terminal states release the slot only once execution is settled. A blocked Run has no queued/live/unresolved work; continuing requires a new proposal. Store a concrete stop reason such as `revision_limit`, `attempt_limit`, `deadline`, `review_uncertain`, or `integrity_failure`, with detail in events.

Ready to merge requires the current Candidate to belong to the frozen Run/contract, an intact recoverable bundle, every configured check passing for the exact base/head and check configuration, and an accepted passing Review for that Candidate's saved context and runner configuration. There must be no cancellation, unresolved execution, conflicting evidence, or exceeded limit. Verify artifact integrity before the completion transaction and recheck identity under lock. Only the controller evaluates this predicate; model prose never determines PWA status.

## Retry, recovery, and evidence applicability

Keep the existing 45-second lease and renewal behavior, process-group termination, worker journal, generation fencing, and reconciliation boundary. Extend them to review and revision. Journal a final report before sending it. Run the server's expiration/recovery sweep periodically without browser activity, covering context preparation and every active phase.

A known timeout, malformed review, or isolated execution failure may retry once only after the prior execution is confirmed stopped and remaining limits permit. Valid judgments, failed objective checks, integrity/policy failures, and unresolved execution are not automatic retry cases. Browser retry requests follow the same rule. Count every Attempt permanently, including failed or lost executions; no accounting settlement is needed.

| Case | Required behavior |
| --- | --- |
| Lost result acknowledgement | Replay the journaled final report. Same Attempt and canonical digest return its durable outcome, even after the workflow advances; create no duplicate Review, Candidate, next Task, or completion event. |
| Restart or lease expiry | Reconcile first. An intact journaled result may advance only if still current, uncancelled, and before deadline. Otherwise preserve evidence without progressing. With no recoverable result, confirm stop before an eligible fresh Attempt. Permanent worker loss remains unresolved until operator recovery. |
| Stale result or wrong Candidate/contract/context | Preserve an identifiable obsolete result as stale; output contradicting its assignment is invalid. Neither can revise or complete the current Candidate. Stale generations cannot advance state. |
| Different second final report for one Attempt | Reject and record the conflicting digest; never overwrite the first report. A conflict affecting current completion evidence blocks delivery/removes readiness after live work stops. |
| Revision cap reached | Preserve the final Candidate and `changes_required` findings; block with `revision_limit`. No new round or retry to evade the cap. |
| Cancellation races with result | Run lock determines order. Once cancellation is recorded, a late result cannot queue revision or declare readiness. Preserve completed evidence. |
| Missing/corrupt artifact | Block delivery and remove readiness when detected. Verify hashes during reconstruction, completion, and retrieval; continuous repository monitoring is unnecessary. |

Every new Candidate requires all checks and a fresh review. Earlier evidence remains inspectable on its original Candidate and cannot qualify its replacement. A changed worker workspace is not the preserved Candidate; a later review never rewrites earlier findings.

## PWA and acceptance

Show the current Candidate, prior Candidates and immutable evidence, round, verdict/findings/uncertainties, remaining Attempts/revisions/time, and stop reason. Preserve bundle download and provide review/context inspection. Label older evidence as historical and render model prose as escaped text. No merge button or pass override.

Retain the existing slice-1 durability/isolation acceptance coverage, updating endpoint expectations for this workflow. Extend the integration suite using real PostgreSQL, artifacts, outbound protocol, journal, and sandboxing, with small deterministic coder/reviewer fixtures:

1. **Delivery:** Candidate → changes required → revision recovered from its bundle → new checks → fresh passing review → Ready to merge. Include direct pass. Assert immutable evidence, lineage, current Candidate, and one next Task across a server restart during context preparation/transition.
2. **Review boundary:** Inspect inputs on both rounds; exclude coder state and prior verdicts. Reject writes, credential access, unauthorized network access, and review Candidate uploads. Exercise stale/wrong identity, incomplete/oversized context, and detected bundle tampering; none can yield readiness.
3. **Recovery:** Lose an acknowledgement and replay without another invocation. Expire a review lease, reconcile before retry, reject an old generation, and race cancellation against revision dispatch. Preserve the existing coding recovery scenario.
4. **Termination:** Exercise a valid blocked review, malformed/timeout review with bounded retry, conflicting reports, revision failure/failed checks, repeated changes required at the revision cap, and Attempt/deadline exhaustion across role changes and restart. Assert project-slot retention until stop confirmation and no promotion from failed or stale evidence.

These are workflow acceptance cases, not a test for every internal branch or reviewer wording. A smoke run with the configured real reviewer must demonstrate that it consumes the pack and returns a usable judgment; model-quality benchmarking and selection are deferred.

## Deferred

Defer provider pricing/cost accounting, tool-call metering, reservation/settlement, generalized execution/reviewer policy schemas, product-quality local-model selection, historical slice-1 Run continuation, and production migration/legacy compatibility machinery beyond actual retained development data.

Also defer merge/deployment, rebase, parallel work, manager inference, additional workers/users, reviewer tools, large-repository retrieval, separate check Tasks, selective evidence reuse, automatic test-failure repair, finding deduplication, model debate, contract edits, and limit extensions. The implementation is one serial loop, one concrete reviewer runner, and one new evidence table.

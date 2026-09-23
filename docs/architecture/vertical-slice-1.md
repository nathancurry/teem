# Vertical slice 1: preserved candidate with objective checks

Status: implementation plan for the smallest end-to-end subset of [v0](v0.md). Stop at a durably preserved Candidate with objective check results. Independent model review and **Ready to merge** belong to the next increment; the slice must not label an unchecked or unreviewed candidate as reviewed.

## Walking path

1. Register one project with a server project ID and a worker-local repository mapping. Configure allowed coding/check operations and objective checks. Use one user, one worker, and one code-changing Run at a time for that repository.
2. In the authenticated text PWA, submit a Request with objective and acceptance criteria. Show a bounded proposal containing repository, base commit, allowed actions, check plan, limits, and delivery condition. Confirm it through a specific decision; persist the proposal, contract, and scoped Approval before queuing work. For this slice, the manager can be a simple server module that presents the structured proposal; a model is unnecessary. A direct instruction may supply authorization only when its scope is explicit and recorded.
3. The controller creates a Run and coding Task. The outbound worker claims one Attempt, records it locally, resolves the project ID, and executes the coder in an isolated workspace under worker-local restrictions. The server records lease and state transitions durably.
4. Commit and freeze the resulting code as a Candidate. Preserve an artifact sufficient to recover that revision independently of the worker, with hash, base/head commits, contract version, and provenance. Run the configured objective checks against the exact Candidate; preserve results and check configuration as evidence.
5. After the artifact and passing check evidence are durable, the controller leaves the Run **Awaiting review**; that is the slice endpoint, not v0 completion. The PWA shows the Candidate and check outcomes. Failed checks, cancellation, or uncertainty show distinct statuses and do not claim success.

## Minimum build order

| Order | Implement | Completion evidence |
| --- | --- | --- |
| 1 | One server process, PostgreSQL schema, durable artifact storage, authenticated text PWA | Request, project, contract, Approval, and Run survive restart |
| 2 | Controller transitions and database task queue | Task and Attempt are distinct; dispatch is recoverable and deduplicated |
| 3 | Versioned outbound worker protocol and local attempt journal | Claim, renew, report, upload, cancel, and reconnect work without inbound worker access |
| 4 | Restricted coder execution and objective checks | Worker resolves only configured projects; supervisor credentials stay outside subprocesses |
| 5 | Candidate preservation and status UI | A fresh server can retrieve the exact code artifact and its check evidence after worker loss |

Use current-state tables plus append-only events in the same transaction. Keep the worker protocol limited to the operations above, with version and capability checks; exact payload shapes, transport timing, and artifact backend can be chosen during implementation. The worker journal supports reconciliation but is not an alternate source of authority. Attempts use stable IDs and increasing assignment generations; stale reports cannot complete a Task. On lease loss, stop work and reconcile before a retry. A retry creates a new Attempt and workspace, never overwrites the previous result.

## Slice acceptance

- From PWA submission through scoped authorization, coding, checks, and Candidate display, every authority transition is recorded; a proposal alone cannot start execution.
- Restarting the server during coding does not lose the Run or create duplicate authoritative work. Duplicate assignment of an Attempt does not launch a second subprocess.
- Disconnecting the worker after code execution but before reporting produces an uncertain state that reconciles on reconnect; it is not silently marked failed or complete.
- Candidate code and objective check evidence remain retrievable after the worker workspace is unavailable. Evidence identifies the exact revision and check configuration.
- No model review, revision loop, merge, deployment, voice, push notification, second user, or second worker is required for this slice.

## Questions to settle while implementing

Choose the concrete authentication, artifact storage, OS isolation, coder adapter, and per-project check configuration before implementing those components. Set explicit lease, timeout, and resource values before accepting work. These choices do not change the authority, durability, or trust boundaries above.

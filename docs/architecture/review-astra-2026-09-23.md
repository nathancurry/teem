# teem architecture review — 2026-09-23

Architectural review summary. Scope: design only; no application implementation.

Start with one control-plane application, one durable database, and an outbound-connected worker daemon. Assume one user, a few projects, trusted enrolled machines, and one active code-changing workflow per repository. Preserve authorization, execution, and evidence boundaries from V1 without splitting them into separate services.

The central rule is that an agent recommending an action does not authorize that action. Observations, proposals, approvals, and execution must remain distinct records and transitions.

The main components are:

| Component | Responsibility | V1 placement |
|---|---|---|
| Phone interface | Conversation, push-to-talk, concise status, decisions, cancellation | PWA |
| Manager | Interpret intent, consult project context, propose bounded work, summarize outcomes | Server module using a configurable model |
| Workflow controller | Own transitions, dispatch, retries, budgets, revision limits, and completion | Server module |
| Policy and approvals | Authorize specific actions against project policy and user decisions | Server module plus worker-local enforcement |
| Project memory | Store requirements, constraints, decisions, provenance, and summaries | Database |
| Worker supervisor | Claim tasks, isolate workspaces, supervise subprocesses, enforce deadlines, report evidence | Worker daemon |
| Agent adapters | Invoke coding and review tools and normalize execution results | Worker modules |
| Model adapters | Invoke configurable inference endpoints | Modules where inference occurs |
| Evidence storage | Preserve candidate code, test results, reviews, and artifact hashes | Durable storage referenced by the database |

Manager/coder/reviewer is a useful conversational model, but needs a deterministic controller. Use manager, executor, verifier, and controller as the architectural roles. The verifier combines ordinary checks with fresh model review for meaningful changes. Models must not enforce their own permissions, retry limits, or completion rules.

Persist the manager through project records and conversation history, rebuilding context for each turn. An immortal provider session is unnecessary. Reviewer independence means a fresh session with the original requirements, exact candidate, relevant source, and verification evidence. Exclude the coder's conversation and self-assessment from the initial review. Different reviewer models are optional; separate machines are unnecessary for ordinary review independence.

Trust boundaries should be enforced as follows:

| Boundary | Rule |
|---|---|
| Phone → server | Authenticate users, deduplicate submissions, bind approvals to specific pending actions |
| Model → controller | Treat outputs as proposals; validate schemas, policy, and current state |
| Server → worker | Explicit enrollment, scoped credentials, allowed projects and capabilities |
| Supervisor → subprocess | Keep worker credentials and privileged operations outside agent access |
| Agent → host/network | Enforce filesystem, credential, network, and resource restrictions outside prompts |
| Repository/tool output → model | Treat content as untrusted input that cannot grant authority |
| Application → provider | Apply project data policy before transmitting context, audio, code, or derived data |

Workers resolve project IDs to locally configured repositories rather than accepting arbitrary host paths. Effective permissions are the intersection of server authorization and worker-local policy.

Use a dedicated worker-owned clone and isolated branches/worktrees. Worktrees share Git data and administrative state; they are not a security boundary. OS isolation must protect the supervisor and host, including when repository tests and build scripts execute. Reviewers can run checks in disposable writable checkouts without permission to modify or publish the candidate. See [Git worktree documentation](https://git-scm.com/docs/git-worktree).

Workers initiate outbound HTTPS long polling to claim tasks, renew leases, upload artifacts, report results, and reconcile after reconnecting. No inbound worker ports or worker-to-worker protocol are needed. Phone commands use HTTPS; foreground status uses SSE or polling. Push notifications prompt a refresh of durable state. Include protocol versions and worker capability checks so old workers can fail cleanly after upgrades.

Use separate durable entities for requests, runs, tasks, attempts, candidates, and approvals. A run is a bounded effort toward an agreed objective; a task is one step; an attempt is one execution of that step. Retries create attempts and revisions create candidates, preserving prior evidence.

The lifecycle is:

1. Record the utterance or text and distinguish observation, question, proposal, and instruction. Ambient input must enter as observation.
2. Persist a proposal with acceptance criteria, repository, base commit, context version, permitted actions, budgets, and completion condition.
3. Apply existing authorization or request a scoped decision. An instruction to implement in a branch can authorize coding and tests without authorizing merge, deployment, or destructive changes.
4. Queue coding, claim an attempt, and execute in an isolated workspace.
5. Freeze an immutable candidate, preserve its code, and run objective checks.
6. Review the exact candidate in a fresh session. Bind evidence to the base/head commits, contract version, check configuration, and relevant environment.
7. Revise within the run's budget. New code or rebasing invalidates applicable review and test evidence.
8. Obtain any required authorization for the final action, perform it, and reconcile the result.
9. Complete only when the agreed delivery condition is satisfied and its artifacts are durable.

For V1, delivery should mean a reviewed, committed candidate and evidence are available, displayed as **Ready to merge**. Automated merge and deployment can follow later. Approvals must identify the action, candidate or artifact, target, and relevant state; a generic “yes” cannot safely resolve multiple pending decisions.

Tasks can use `queued → leased → running → succeeded/failed/canceled`, with an explicit `uncertain` outcome when execution cannot be determined. Keep connection, approval, and review state separate. Commit each accepted state transition and its domain event atomically. Use state tables plus append-only history; defer full event sourcing.

Durability should follow recovery and authority requirements:

| Durable | Ephemeral or replaceable |
|---|---|
| Requests and retained transcripts | Live microphone buffers |
| Project constraints, decisions, and provenance | Reconstructed model context and caches |
| Versioned contracts, policy, and approvals | Token streams and typing indicators |
| Runs, tasks, attempts, leases, deduplication keys | Connections and expendable heartbeat samples |
| Budgets, usage, deadlines | UI projections |
| Candidate bundles/commits and artifact hashes | Checkouts after artifact preservation |
| Test evidence and review verdicts | Verbose logs after retention expires |
| Pending external actions and outcomes | Notification transport sessions |
| Pending notifications | Other rebuildable presentation state |

The worker needs a small local journal of accepted attempts, execution identities, workspace locations, and unsent results. This supports reconciliation without becoming a second authority. Preserve candidate artifacts before marking delivery complete; a commit available only on a worker is insufficient. A Git bundle in server artifact storage is adequate initially.

Store explicit decisions separately from generated summaries. Summaries should reference sources and must not silently become policy. Use short raw-audio retention by default. Privacy routing must cover transcripts, summaries, logs, and embeddings as well as source code. Local coding does not make a workflow private if the manager sends sensitive context elsewhere.

The unavoidable distributed-systems problem is uncertain execution. Leases and increasing assignment generations prevent stale attempts from updating authoritative state, but do not prove old processes have stopped. Supervisors should stop work when lease renewal fails within the permitted window. Keep privileged external effects outside unrestricted agent control.

| Failure | Required response |
|---|---|
| Worker disconnects | Separate connection loss from task failure; reconcile before deciding the outcome |
| Duplicate delivery | Deduplicate by attempt identity; do not launch another subprocess for an accepted attempt |
| Lost worker | Retry isolated coding in a new workspace; supersede the old attempt |
| Late stale result | Preserve evidence if useful, reject authoritative transitions |
| Manager/server crash | Resume durable workflow state; prevent duplicate dispatch through unique transition/task identities |
| Lost model response | Bound retries and account for potentially incurred charges |
| Stale review | Recheck and review the new candidate as applicable |
| Changed target branch | Reconcile target state and revalidate affected evidence |
| Conflicting commits | Serialize code-changing runs per repository in V1; still check changes from humans |
| Agent loop | Enforce run-level revision, time, tool-call, and cost limits |
| Partial external action | Inspect the external system using a stable operation identity before retrying |
| Cancellation during partition | Record cancellation immediately; show execution as unconfirmed until reconciled |
| Artifact/notification failure | Retry delivery independently without rerunning coding |

Persist an operation record before external effects and reconcile afterward. When an external action has neither idempotency nor a reliable result query, uncertainty must stop automatic retries. Do not promise exactly-once execution.

Start with one coder, one reviewer, and at most two revision rounds. Budgets cover the entire run, including manager calls, reviews, retries, and revisions. Enforce deadlines and resource limits outside models. Financial caps remain estimates unless the provider offers enforceable limits.

A monorepo is appropriate because protocol, worker, UI, and workflow changes will often be atomic:

| Path | Recommendation |
|---|---|
| `apps/web` | Keep |
| `apps/server` | Own orchestration, policy, persistence, and manager behavior |
| `services/worker` | Keep independently installable and upgradeable |
| `packages/protocol` | Share versioned messages and validation; exclude database internals |
| `packages/providers` | Extract when shared; distinguish inference adapters from agent execution adapters |
| `packages/state` | Initially keep within the server |

Configure provider/model selection by role and project and record it per attempt. Preserve backend differences in cancellation, tooling, state, and usage reporting. One working implementation per adapter category is sufficient initially; a universal agent framework is unnecessary.

Use one PostgreSQL database for state, task queuing, and a notification outbox, plus durable artifact storage. Run the scheduler in the server process and recover pending work after restart. PostgreSQL supports transactional queue consumers using `SKIP LOCKED`; a separate broker is unnecessary initially. See [PostgreSQL documentation](https://www.postgresql.org/docs/current/sql-select.html).

Avoid separate manager/reviewer/memory services, replicated worker state, arbitrary workflow languages, general delegation graphs, concurrent coding branches, automatic merge queues, and vector databases before ordinary records and text search prove insufficient.

The phone should show what is happening, whether a decision is needed, and what happens next. Summarize failures and revisions in ordinary language with evidence available on demand. Use a specific pending decision card and deliberate authenticated confirmation for high-risk actions. Background speech must not authorize them.

Push-to-talk fits V1. iOS Web Push supports installed Home Screen web apps and permission requests require direct user interaction; account for this in onboarding. See [WebKit documentation](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/).

Build incrementally:

| Stage | Deliver |
|---|---|
| First vertical slice | Text PWA, authenticated server, project records, one worker, durable attempts, isolated coding, checks, preserved candidate |
| Useful V1 | Manager proposals, scoped authorization, independent review, bounded revisions, reconnect/cancellation handling, concise status, push-to-talk, notifications |
| Later | Automated merge with stale-state checks, more workers/providers, local inference with explicit data policy |
| Explicitly deferred | Deployment automation, multi-repository workflows, parallel coding, continuous voice, AirPods guarantees, ambient capture, multi-user tenancy, autonomous destructive operations |

Before calling V1 reliable, demonstrate server restart during coding, worker disconnection after execution but before reporting, duplicate assignment delivery, and candidate modification after review. These validate the recovery and authority boundaries most difficult to retrofit.

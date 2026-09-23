 Build teem as one control-plane application, one durable database, and an outbound-connected worker daemon. Keep the manager as the user’s conversational interface, but put scheduling, authorization, retries, and completion rules in ordinary application code.

The most important architectural distinction is between an agent recommending an action and the system authorizing that action. Preserve that distinction from V1.

Assumptions for this recommendation: one user, a few projects, trusted enrolled machines, and one active code-changing workflow per repository.

The major components should be:

Component                     Responsibility                                                                             Placement in V1
━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Phone interface               Conversation, push-to-talk, concise status, decisions, cancellation                        PWA
────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────────────────
Manager                       Interpret requests, consult project context, propose work, explain results                 Server module calling a configurable model
────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────────────────
Workflow controller           Validate transitions, schedule tasks, enforce budgets, manage retries and revisions        Server module
────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────────────────
Policy and approval module    Decide whether a specific action is authorized                                             Server module, with additional worker enforcement
────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────────────────
Project memory                Requirements, decisions, constraints, source references, summaries                         Database
────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────────────────
Worker supervisor             Claim tasks, manage subprocesses, isolate workspaces, enforce deadlines, report results    Worker daemon
────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────────────────
Agent adapters                Invoke coding/review tools and normalize their results                                     Worker modules
────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────────────────
Model adapters                Invoke models for conversation and other inference                                         Modules where inference happens
────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────  ───────────────────────────────────────────────────
Evidence storage              Preserve commits or bundles, diffs, test results, review reports                           Durable storage with database references

These are responsibility boundaries. Most do not need separate processes.

flowchart LR
P[Phone PWA] -->|HTTPS requests| S[Control plane]
S -->|Status and decisions| P
S --- D[(Database)]
S --- A[(Artifacts)]
S --> M[Configured model endpoint]
W[Worker supervisor] -->|Outbound HTTPS: claim, renew, report| S
W --> C[Coder sandbox]
W --> R[Fresh reviewer sandbox]
W --> T[Verification commands]

The manager/coder/reviewer decomposition is useful as a product model, but incomplete as an execution architecture.

The missing role is the deterministic workflow controller. A manager model should not decide whether its own budget has expired, whether an approval remains valid, or whether a disconnected worker can safely be replaced.

I would use four conceptual roles:

Manager: understands intent and proposes bounded work.

Executor: changes code within an authorized scope.

Verifier: runs objective checks and performs independent review.

Controller: owns permissions, transitions, retries, and completion.

The verifier combines conventional checks with an optional reviewer model. Formatting, compilation, tests, and policy checks should not require agent deliberation. For meaningful code changes, add a fresh reviewer invocation after those checks.

The manager’s persistence should come from stored project state and conversation history. It does not need to be an immortal model session. Reconstruct its context for each turn; provider sessions can be an optimization.

Reviewer independence should mean a fresh session receiving the original requirements, acceptance criteria, exact candidate revision, relevant source, and verification evidence. Have it form its initial assessment without the coder’s conversation or self-assessment. Different models can reduce correlated mistakes, but should remain optional. A separate machine is unnecessary for ordinary review independence.

Treat trust boundaries separately from communication boundaries.

Boundary                          Required rule
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Phone → server                    Authenticate the user; deduplicate submissions; bind approvals to a specific pending action
────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────
Model → controller                Model output is a proposal that must pass schema, policy, and state validation
────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────
Server → worker                   Enroll workers explicitly; scope credentials to allowed projects and capabilities
────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────
Supervisor → agent subprocess     Keep worker credentials and privileged operations outside the agent’s environment
────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────
Agent → filesystem/network        Enforce allowed mounts, credentials, destinations, and resource limits outside prompts
────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────
Repository/tool output → model    Treat content as potentially hostile input; it cannot grant permissions
────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────
Application → model provider      Apply project data policy before sending context, source, transcripts, or logs

Workers should resolve a project identifier to a locally configured repository. A job should not be able to name an arbitrary host path and gain access to it.

Effective permissions should be the intersection of server authorization and worker-local policy. A home worker and a work machine can therefore accept different projects and operations.

Worktrees provide workspace isolation, not a security boundary. Git worktrees share repository data and some administrative state. Use a dedicated worker-owned clone, then worktrees inside that clone; protect the supervisor and host through OS isolation. Repository tests and build scripts also execute code and need the same treatment. Git worktree documentation (https://git-scm.com/docs/git-worktree)

A reviewer can inspect immutable source while running tests in a disposable writable checkout. It should have no authority to publish or modify the candidate under review.

For communication, start with worker-initiated HTTPS long polling:

Claim an eligible task.

Renew its lease and report progress.

Upload artifacts.

Submit the final result.

Reconcile outstanding attempts after reconnecting.

Use ordinary HTTPS for phone commands and SSE or polling for foreground updates. Push notifications should tell the phone to refresh durable state. They are not the authoritative delivery mechanism.

Include protocol versions and capability negotiation immediately: a sleeping worker may reconnect after a server upgrade. You do not need elaborate compatibility machinery, but incompatible workers must be refused cleanly.

Distinguish the user’s request, the overall workflow, and each execution attempt.

Use these durable concepts:

Concept      Meaning
━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Request      User input, its transcript, and relevant conversation context
───────────  ───────────────────────────────────────────────────────────────
Run          One bounded effort to satisfy an agreed objective
───────────  ───────────────────────────────────────────────────────────────
Task         A coding, verification, review, or finalization step
───────────  ───────────────────────────────────────────────────────────────
Attempt      A particular execution of a task on a worker
───────────  ───────────────────────────────────────────────────────────────
Candidate    Immutable code revision plus its evidence
───────────  ───────────────────────────────────────────────────────────────
Approval     Permission for a specific action and scope

Retries create attempts; revisions create new candidates. Neither should overwrite the history of what previously happened.

A suitable lifecycle is:

┌──────────────────────────────────────────┐
│ Utterance or text                        │
│                                          ├───────────────────────────────────────┐
└──────────────────────────────────────────┘                                       │
│
│
┌──────────────────────────────────────────┐                                       │
│ Interpret intent                         │                                       │
│                                          ├◄──────────────────────────────────────┘
│                                          ├─────────────────────────────────────────┐
└──────────────────────────────────────────┘                                         │
│
│
┌──────────────────────────────────────────┐                                         │
│ Persist proposal and acceptance criteria │                                         │
│                                          ├◄────────────────────────────────────────┘
│                                          ├───────────────────────────────────────────┐
└──────────────────────────────────────────┘                                           │
│
│
┌──────────────────────────────────────────┐                                           │
│ ◇ Authorized?                            │                                           │
│                                          ├◄──────────────────────────────────────────┘
│                                          ├──Needs decision─────────────────────────────┐
│                                          ├◄────────────────────────────────────────────╪─┐
│                                          ├──Yes────────────────────────────────────────╪─╪─┐
└──────────────────────────────────────────┘                                             │ │ │
│ │ │
│ │ │
┌──────────────────────────────────────────┐                                             │ │ │
│ Await user approval                      │                                             │ │ │
│                                          ├◄────────────────────────────────────────────┘ │ │
│                                          ├───────────────────────────────────────────────┘ │
└──────────────────────────────────────────┘                                                 │
│
│
┌──────────────────────────────────────────┐                                                 │
│ Queue coding task                        │                                                 │
│                                          ├◄────────────────────────────────────────────────┘
│                                          ├───────────────────────────────────────────────────┐
└──────────────────────────────────────────┘                                                   │
│
│
┌──────────────────────────────────────────┐                                                   │
│ Execute in isolated workspace            │                                                   │
│                                          ├◄──────────────────────────────────────────────────┘
│                                          ├─────────────────────────────────────────────────────┐
│                                          ├◄────────────────────────────────────────────────────╪───┐
└──────────────────────────────────────────┘                                                     │   │
│   │
│   │
┌──────────────────────────────────────────┐                                                     │   │
│ Freeze candidate and run checks          │                                                     │   │
│                                          ├◄────────────────────────────────────────────────────┘   │
│                                          ├───────────────────────────────────────────────────────┐ │
└──────────────────────────────────────────┘                                                       │ │
│ │
│ │
┌──────────────────────────────────────────┐                                                       │ │
│ Independent review                       │                                                       │ │
│                                          ├◄──────────────────────────────────────────────────────┘ │
│                                          ├──Changes required, budget available─────────────────────┘
│                                          ├──Budget exhausted or unresolved───────────────────────────┐
│                                          ├──Pass─────────────────────────────────────────────────────╪─┐
└──────────────────────────────────────────┘                                                           │ │
│ │
│ │
┌──────────────────────────────────────────┐                                                           │ │
│ Blocked for decision                     │                                                           │ │
│                                          ├◄──────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────┘                                                             │
│
│
┌──────────────────────────────────────────┐                                                             │
│ ◇ Final action authorized?               │                                                             │
│                                          ├◄────────────────────────────────────────────────────────────┘
│                                          ├──No───────────────────────────────────────────────────────────┐
│                                          ├◄──────────────────────────────────────────────────────────────╪─┐
│                                          ├──Yes──────────────────────────────────────────────────────────╪─╪─┐
└──────────────────────────────────────────┘                                                               │ │ │
│ │ │
│ │ │
┌──────────────────────────────────────────┐                                                               │ │ │
│ Await scoped approval                    │                                                               │ │ │
│                                          ├◄──────────────────────────────────────────────────────────────┘ │ │
│                                          ├─────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────┘                                                                   │
│
│
┌──────────────────────────────────────────┐                                                                   │
│ Perform and reconcile final action       │                                                                   │
│                                          ├◄──────────────────────────────────────────────────────────────────┘
│                                          ├─────────────────────────────────────────────────────────────────────┐
└──────────────────────────────────────────┘                                                                     │
│
│
┌──────────────────────────────────────────┐                                                                     │
│ Complete with evidence                   │                                                                     │
│                                          ├◄────────────────────────────────────────────────────────────────────┘
└──────────────────────────────────────────┘

Several rules make this lifecycle reliable:

Classify the utterance before creating work. An observation, a question, a proposal, and an instruction have different consequences. “This screen feels slow” can remain an observation until intent is clear. Future ambient capture must enter through the observation path.

Freeze the execution contract. Before coding, record the objective, acceptance criteria, repository, base commit, permitted actions, context version, budgets, and completion condition. Material changes create a new contract version.

Apply standing authorization where appropriate. “Implement this in an isolated branch” can authorize coding and tests immediately. It does not automatically authorize deployment, deletion, or changes to unrelated repositories. Avoid making the user approve every harmless step.

Bind review to a candidate. Record the base and head commit, contract version, check configuration, and relevant environment. Editing or rebasing the candidate invalidates the applicable review and test evidence.

Bind approval to the action. A merge approval should identify the candidate and target branch state. A deployment approval should identify the artifact and environment. A generic “yes” is insufficient when several decisions are pending.

Define completion before execution. For V1, I recommend “a reviewed, committed candidate and its evidence are durably available.” Display that as Ready to merge. Automated merging and deployment can arrive later.

A task can use queued → leased → running → succeeded/failed/canceled, with an explicit uncertain outcome when execution cannot be determined. Keep review state, approval state, and connection state separate instead of creating dozens of combined statuses.

For each accepted transition, update the current state and append its domain event in one database transaction. Events might include ProposalCreated, AuthorizationRecorded, AttemptStarted, CandidateRecorded, ReviewRecorded, and RunCompleted.

Use ordinary state tables plus an append-only history. Full event sourcing would add replay, migration, and compatibility obligations without a clear V1 benefit.

Persist everything needed to recover authority, explain decisions, or preserve accepted work.

Durable                                              Ephemeral or replaceable
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Requests and retained transcripts                    Live microphone buffers
───────────────────────────────────────────────────  ──────────────────────────────────────────────────
Project constraints and decision history             Typing indicators and token streams
───────────────────────────────────────────────────  ──────────────────────────────────────────────────
Versioned proposals and acceptance criteria          Reconstructed model context
───────────────────────────────────────────────────  ──────────────────────────────────────────────────
Policy versions, permissions, approvals              Network connections
───────────────────────────────────────────────────  ──────────────────────────────────────────────────
Runs, tasks, attempts, leases, deduplication keys    Heartbeat samples beyond operational retention
───────────────────────────────────────────────────  ──────────────────────────────────────────────────
Budget reservations, consumption, deadlines          Rebuildable caches
───────────────────────────────────────────────────  ──────────────────────────────────────────────────
Candidate commits/bundles and artifact hashes        Disposable checkouts after artifact preservation
───────────────────────────────────────────────────  ──────────────────────────────────────────────────
Test evidence and review verdicts                    Verbose logs after their retention period
───────────────────────────────────────────────────  ──────────────────────────────────────────────────
Pending external actions and their outcomes          Notification delivery sessions
───────────────────────────────────────────────────  ──────────────────────────────────────────────────
Pending notifications                                UI projections that can be rebuilt

Store explicit decisions separately from generated summaries. Summaries should point to their sources and never silently become policy. Keep project facts, user decisions, and agent hypotheses distinguishable.

The worker also needs a small durable local journal: accepted attempts, workspace locations, subprocess execution identities, and unsent results. It supports reconciliation; it does not become a second authority for project state.

Upload the candidate and evidence before declaring delivery complete. A commit existing only on a laptop is not durable delivery. A Git bundle in server artifact storage is a sufficient starting mechanism.

Use short raw-audio retention by default. Provider routing must account for transcripts, summaries, logs, and embeddings as well as source code: derived material may retain the original sensitivity. A local coding model does not make a workflow private if the manager sends its context elsewhere.

The unavoidable distributed-systems problem is uncertain execution. Design that carefully and keep everything else simple.

A lease tells the server which attempt may report authoritative progress. It does not prove that an old process has stopped.

Give each assignment an increasing generation number and reject state updates from superseded generations. The supervisor should stop work when it cannot renew authorization within the lease window. A disconnected or compromised process may still continue, so privileged external effects must remain outside its unrestricted control.

Failure                                                Required behavior
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Worker disconnects                                     Show connection loss separately from task failure. Stop assigning new work; reconcile the existing attempt on reconnect
─────────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Duplicate task delivery                                Deduplicate by attempt identity in the worker journal; never launch a second subprocess for the same accepted attempt
─────────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Worker lost during coding                              Preserve available checkpoints; retry only in a new isolated workspace, with the old attempt superseded
─────────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Old worker reports late                                Retain its evidence for inspection, but reject authoritative transitions from its stale generation
─────────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Manager or server crashes                              Resume from durable workflow state; unique transition/task identities prevent duplicate dispatch
─────────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Model response is lost                                 Retry only within budget; provider charges may already have occurred
─────────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Review becomes stale                                   Require applicable checks and review for the new candidate
─────────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Target branch changes                                  Reconcile against the new target; revalidate affected evidence before integration
─────────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Two changes conflict                                   Serialize code-changing runs per repository in V1; still verify target state because humans may commit independently
─────────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Agent loop fails to converge                           Stop at persisted revision, time, tool-call, and cost limits; escalate with a concise explanation
─────────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
External action succeeds but acknowledgment is lost    Inspect the external system using a stable operation identity; do not blindly repeat
─────────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Cancellation arrives during a partition                Record cancellation immediately, show execution as unconfirmed, and reconcile later
─────────────────────────────────────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Artifact upload or notification fails                  Retry independently; neither should cause coding to run again

Separate retryable isolated computation from externally visible actions. Repeating a coding attempt in another workspace is wasteful but manageable. Repeating a migration, deployment, payment, or destructive command may be unacceptable.

For those actions, persist an operation record before execution and reconcile the observed result afterward. If an external system offers neither idempotency nor a reliable way to inspect the result, uncertainty must stop automatic retries.

Set limits at the run level, including manager calls, reviews, retries, and revisions. Otherwise each new task resets the budget. Start with one coder, one reviewer, and at most two revision rounds. Enforce subprocess deadlines and resource limits outside the model. Treat financial caps as estimates unless the provider exposes enforceable spending controls.

Start with a monorepo. The protocol, workflow rules, worker behavior, and UI will change together frequently. Atomic changes and shared contract tests outweigh independent release concerns at this stage.

Your proposed structure is reasonable, with two adjustments:

Path                  Recommendation
━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
apps/web              Keep
────────────────────  ────────────────────────────────────────────────────────────────────────────────────────────
apps/server           Keep; own orchestration, policy, persistence, and manager behavior here
────────────────────  ────────────────────────────────────────────────────────────────────────────────────────────
services/worker       Keep; independently installable and upgradeable
────────────────────  ────────────────────────────────────────────────────────────────────────────────────────────
packages/protocol     Keep; versioned messages and validation, with no database dependencies
────────────────────  ────────────────────────────────────────────────────────────────────────────────────────────
packages/providers    Add when genuinely shared; distinguish model APIs from coding-tool execution adapters
────────────────────  ────────────────────────────────────────────────────────────────────────────────────────────
packages/state        Initially keep inside the server; workers and browsers should not share database internals

Do not turn a provider adapter into a universal agent framework. A model API and a coding CLI have different cancellation, tool execution, state, and usage behavior. Preserve those capabilities explicitly rather than pretending every backend supports the same contract.

Configure model/provider selection by role and project, and record the selection used for each attempt. One working implementation of each adapter category is enough initially.

The premature complexity to avoid is:

Separate services for the manager, scheduler, reviewer, memory, and approvals.

A message broker alongside a database before database queuing becomes a measured problem.

Worker-to-worker communication or replicated project state.

A general workflow language or arbitrary agent delegation graph.

Concurrent coding branches and an automated merge queue.

Vector databases before ordinary project records and text search prove inadequate.

Automatic failover across models with different privacy or capability guarantees.

Treating voice streaming and continuous capture as prerequisites for useful operation.

I would use one PostgreSQL database for state and the task queue, plus durable artifact storage. Queue claims can use transactional row locking; PostgreSQL explicitly documents SKIP LOCKED for queue-like consumers. No Redis or broker is necessary initially. PostgreSQL documentation (https://www.postgresql.org/docs/current/sql-select.html)

Run the workflow scheduler inside the server process initially. Commit pending work before performing remote calls, and scan durable pending work after restart. A transactional notification outbox can live in the same database.

Make the phone experience a projection of this durable workflow. Each run should answer three questions: what is happening, whether the user needs to decide anything, and what happens next.

Present short summaries such as: “The change passes tests. Review found one permissions issue; revision one of two is running.” Keep detailed evidence available without making terminal logs the primary interface.

Use one pending decision card with explicit options and a recommendation. For high-risk approvals, require a deliberate authenticated confirmation tied to that card; background speech should never approve it.

Push-to-talk fits V1. On iOS, Web Push supports installed Home Screen web apps and permission requests require direct user interaction, so onboarding should account for that. WebKit documentation (https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)

Build incrementally in this order:

Stage                   Deliver
━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
First vertical slice    Text PWA, authenticated server, project records, one worker, durable attempts, isolated coding, checks, preserved candidate
──────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Useful V1               Manager proposals, scoped authorization, fresh review, bounded revisions, reconnect/cancellation handling, concise status, push-to-talk and notifications
──────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Later                   Automated merge with stale-state checks; additional workers and providers; local inference under explicit data policy
──────────────────────  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
Explicitly deferred     Deployment automation, multi-repository workflows, parallel coding, native continuous voice, AirPods interaction guarantees, ambient capture, multi-user tenancy, autonomous destructive operations

Before calling V1 reliable, demonstrate recovery from four scenarios: kill the server during coding, disconnect the worker after execution but before reporting, deliver the same assignment twice, and change the candidate after review. Those exercises test the boundaries that will be hardest to retrofit.

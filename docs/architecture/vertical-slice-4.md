# Vertical slice 4: Telegram decider, real agents, GitHub pull requests

Status: proposed increment after [slice 3](vertical-slice-3.md). It replaces the phone PWA as the primary interface and replaces the placeholder coder and local reviewer with real coding agents.

## Outcome and scope

```text
Telegram voice note or text
  → local transcription (voice) → decider turn with recent conversation and Run status
  → reply / clarifying question / proposed Run
  → granted project: controller approves under the grant
    ungranted project: Allow/Deny buttons, then approval
  → implementer (Claude Code) → checks → adversary (Codex) → bounded revision
  → server pushes the exact reviewed Candidate to a teem/* branch and opens a GitHub PR
  → Telegram check-in with the PR link
```

Keep PostgreSQL, the controller transitions, Run/Task/Attempt/Candidate/Review records, leases, the worker journal, reconciliation, artifact preservation, and the Run → Task → Attempt lock order. The worker protocol keeps its operations; bump it to version 3 for the changed assignment and review payloads.

The user can merge from GitHub. Teem does not merge, deploy, push to non-`teem/` branches, or track what happens to a PR after it opens.

## Telegram channel

Use one bot through the official Bot API. The server long-polls `getUpdates` over outbound HTTPS, so there are no webhooks and no inbound port. Configure the bot token and one numeric Telegram user ID. The server ignores updates, including callback queries, from any other user and from any chat that is not the private chat with that user.

Store each accepted update in `telegram_updates` keyed by `update_id` before acknowledging it through the next `getUpdates` offset. Process pending updates in order. Processing is the only step that makes model calls; it happens outside database transactions. The resulting effects and `processed_at` commit together. A crash after the model call repeats the call. The existing Request deduplication key `tg:<update_id>` keeps that repeat from creating a second Run.

Voice notes are OGG/Opus. Download the file through the Bot API, then transcribe it with the slice-3 runner, extended to accept Ogg/Opus. Replace `ggml-base.en.bin` with a pinned, hash-verified larger English model chosen after a latency measurement on the TrueNAS CPU. Raise the clip bound to 120 seconds and set the inference bound from that measurement. Always echo the transcript first ("Heard: …") so misrecognitions are visible. A transcription failure produces a reply asking the user to resend or type; it never becomes a decider turn. Voice notes and all messages pass through Telegram's servers; bot chats are not end-to-end encrypted. The slice-3 promise that no external service receives audio no longer holds.

Outbound messages go through a `telegram_outbox` sender loop in the server. It reuses the slice-3 delivery pattern: reserve an attempt durably before the HTTP call, retry transport failures and `429` (honoring `retry_after`) up to six attempts, and accept a rare duplicate message after a crash. Messages may contain project names, objectives, summaries, and links, but never credentials.

## Decider

The decider is a server module. Each processed update triggers one model call through OpenRouter. Server configuration fixes the model ID and a timeout. The server rebuilds the context on every turn:

- the last 30 inbound and sent messages;
- granted and proposed projects;
- active Runs and Runs finished in the last 24 hours, with server-derived status labels, stop reasons, and PR links.

Source code, diffs, and check output are not sent to the decider. Adversary summaries and stop reasons are.

The model's text content is sent as the reply. Beyond that, the decider acts only through these tools. The server validates each call against current state and rejects invalid calls without side effects:

| Tool | Effect |
| --- | --- |
| `propose_run(repo, objective, acceptance_criteria)` | Create the project row if needed, then a Request (`tg:<update_id>`), Run, and contract version 1. For a granted repository, the controller records an Approval with source `project_grant:<grant_id>` and queues the Task in the same transaction. Otherwise, the Run waits for an Approve/Deny button, which approves only that Run. |
| `propose_project(repo)` | Create or reuse the project row for a GitHub `owner/name` inside the configured owner allowlist, give it a fresh grant ID, and send Allow/Deny buttons. |
| `cancel_run(run_id)` | Apply the existing cancellation transition. |

Ask clarifying questions in the reply text. The user's next message arrives with the conversation history; no pending-question record is needed. A turn may call at most one tool. Invalid tool calls and model timeouts produce a short error reply.

The decider cannot approve, grant, revoke, merge, change limits, or edit a contract. Words such as "yes", "approve", or "merge" have no decision meaning in text or voice; only callback buttons decide.

## Ad hoc project grants

A project is a GitHub repository. Replace the static registration (fixed base commit and server-side checks) with grants:

1. The user asks Teem to work on a repository. The decider calls `propose_project`.
2. The bot sends "Allow Teem to code in `owner/name`?" with **Allow** and **Deny** buttons. The callback data carries the project row UUID. The server checks the callback sender, the pending state, and the owner allowlist, then records the decision.
3. A granted project authorizes, for any future Run on that repository: code, check, review, and revise; pushing to `teem/<run id>`; and opening one PR against the default branch. It never authorizes merging, other branches, releases, repository settings, or other repositories.
4. `/revoke owner/name`, typed as a bot command and parsed by the server rather than the decider, revokes the grant. Revocation blocks new Runs and publication. An in-flight Run stops as `blocked` before publishing.

Each Run's contract records its base: the default-branch head at proposal time. The server resolves it by fetching its own mirror clone of the repository, using its GitHub token for private repositories. Checks come from `.teem/checks.json` at that base commit. The contract records their hash, as in slice 2. If the file is missing, the contract records an empty check list, and the proposal message and PR body say so. The coder cannot change the checks for its own Run, because the checks are read from the base commit.

Worker-local policy remains an intersection. `projects.json` becomes a list of allowed GitHub owners plus the agent image and credential paths. The worker refuses assignments outside those owners.

## Execution

### Sandbox

Replace Bubblewrap coder, check, and review sandboxes with one rootless Podman container per subprocess. The container is named after its Attempt, so a restarted worker can check with `podman ps` whether the process still exists. Its mounts and access:

- The workspace is the only read-write bind mount. The contract and context files are mounted read-only.
- It connects to an internal network whose only exit is an allowlisting forward proxy container, stock tinyproxy with default-deny. The allowlist covers the model API hosts for the two CLIs and the package registries the projects need. It excludes github.com write paths.
- It receives the model credential its role needs and no other secrets. The GitHub tokens never enter any container.
- CPU, memory, PID, and wall-clock limits are enforced by Podman. Timeouts use `podman kill`.

The agent can still read and misuse its own model credential, and it can exfiltrate data to allowlisted hosts. That is accepted for one user on a dedicated worker. The container is not a VM, so do not run it on the TrueNAS host. Build the agent image from `deploy/worker/agent.Containerfile` with pinned CLI and toolchain versions.

### Implementer

Run Claude Code in headless mode (`claude -p`) against the Claude subscription. Authenticate with a one-year token from `claude setup-token`, stored in worker-local configuration and passed into the implementer container only, as `CLAUDE_CODE_OAUTH_TOKEN`. The token can make model requests only. The worker writes the prompt from the contract:

- objective and acceptance criteria;
- check commands;
- for revisions, the accepted review findings and the failed check evidence.

Pin the exact flags with the CLI version. Permissions are bypassed inside the container, and a turn cap applies. The supervisor commits and bundles as before. The implementer's final message is stored as `implementer_summary` evidence and shown in the PR body. It is never given to the adversary.

If the implementer makes no code changes, the Run stops as `blocked`, with the final message as the stop reason. This lets the implementer "ask" instead of guessing. The decider relays the message, and the user's answer leads to a new proposal and a new Run.

### Checks

Checks run in the agent image with the proxy and no model credentials. **Failing checks now trigger a revision** instead of stopping the Run. The revision consumes a round and carries the failed check evidence to the implementer. Failing checks at the revision cap stop the Run as `checks_failed`.

### Adversary

Run Codex non-interactively against the ChatGPT subscription credential, with its final output constrained to the existing review JSON schema. It works in a disposable writable clone of the Candidate with no model context from the implementer. Its input is the same fresh review context as slice 2: contract, Candidate source, and check evidence. It may run tests and write probe tests.

Its changes are discarded, but a finding may attach one bounded reproduction as evidence: a test file up to 16 KiB and its output. The next revision receives that reproduction. Existing review validation, stale-review rules, and verdict semantics are unchanged. Remove the Ollama bridge and `reviewer_ollama.py`.

### Limits

| Limit | Slice-4 value |
| --- | --- |
| Implementer subprocess | 45 minutes |
| Each check | 10 minutes |
| Adversary subprocess | 20 minutes |
| Run deadline | 3 hours from approval, never reset |
| Revisions | Default 2, cap 2, whether triggered by review or by checks |
| Attempts | 8 per Run, at most 2 per Task |

A subscription usage-limit error is an ordinary failed Attempt. Its error text reaches the user through the decider. Provider fallback is deferred.

## Publication

When the controller reaches `ready_to_merge`, it sets the Run to `publishing` in the same transaction. The server's publisher loop handles `publishing` Runs:

1. Fetch the preserved Candidate bundle into a server-owned bare mirror of the repository.
2. Force-push the Candidate head commit to `teem/<run id>`. This is idempotent.
3. Look up an open PR whose head is that branch, and create one only if none exists.
4. Commit `pr_url` and status `pr_open`, then queue the check-in message.

The PR body contains the objective, criteria, check results, the implementer summary, and the adversary's verdict. The server pushes from the preserved artifact, so the PR contains exactly the reviewed Candidate. Both steps can be looked up before retrying, so a crash anywhere in the loop is safe to resume. A revoked grant or a GitHub permission error stops the Run as `blocked` without retrying.

Credentials:

- The server holds one fine-grained GitHub token for the allowed owner with contents read/write and pull request read/write. Server code pushes only `teem/*` refs.
- The worker holds a read-only token for cloning.

## Check-ins

The sender queues a Telegram message only for:

- the transcript echo and decider replies;
- a pending project grant or Run approval (with buttons);
- Run started (one line);
- PR opened (link and two-line summary);
- a stop: `blocked`, `checks_failed`, `failed`, `uncertain`, a disagreement still open at the revision cap, or a limit reached.

Normal coding, check, review, and revision progress sends nothing. The user can ask "what's going on?" at any time.

## Durable changes

Add explicit SQL in `docs/architecture/slice-4.sql`:

| Table | Change |
| --- | --- |
| `projects` | The key becomes the lowercase GitHub `owner/name`. Add unique `grant_id uuid` (carried by Allow buttons and replaced on each new prompt), `status` (`proposed`, `granted`, `denied`, `revoked`), and `decided_at`. Drop `base_commit`, `checks`, and `check_hash`; contracts already carry them. Existing slice-3 projects become `revoked`. |
| `runs` | Add nullable `pr_url`; add statuses `publishing` and `pr_open`. |
| `telegram_updates` | `update_id bigint PRIMARY KEY`, `kind` (`text`, `voice`, `callback`, `unsupported`, `ignored`), `payload jsonb`, nullable `text` (message or transcript), `received_at`, nullable `processed_at`. |
| `telegram_outbox` | `id`, nullable `run_id`, nullable `project_id`, nullable `text`, `state` (`pending`, `sent`, `abandoned`), `attempt_count`, `next_attempt_at`, nullable `telegram_message_id`. A row with a Run or project and no text is composed when sent, from current state, with buttons only if the decision is still pending. |

Drop `push_subscriptions` and `notification_deliveries`. Remove the Web Push sender, `sw.js` push handling, VAPID configuration, and the PWA dictation UI. Keep the authenticated HTML Run and evidence pages for inspecting detail behind a link; they get no further investment.

## Acceptance

Keep the slice-1/2 recovery regressions that still apply. Use real PostgreSQL and HTTP, fake Telegram and GitHub endpoints, and scripted stand-ins for the two CLIs inside the real Podman sandbox:

1. **Voice to PR:** a voice note is transcribed and echoed. The decider proposes work on an ungranted repository, and **Allow** grants it. A second request on that repository starts without buttons. A first Candidate fails checks and is revised. The adversary requests changes with a reproduction, and the revised Candidate passes. The branch and PR contain exactly the preserved Candidate, and a check-in reaches the user.
2. **Authority:** transcripts saying "approve" or "merge" decide nothing. Callbacks from other users, stale Run versions, and repositories outside the owner allowlist are rejected. A decider tool call naming an ungranted repository cannot start work. Revocation stops publication. No GitHub token is visible inside any container.
3. **Durability:** a restart after update receipt, before processing, produces one Run. A restart during publishing produces one branch and one PR. The outbox resumes after a restart.
4. **Sandbox:** a container cannot reach a non-allowlisted host or the TrueNAS/LAN addresses. After a worker restart, reconciliation sees a still-running container and does not launch a second one.

Manual acceptance, once with real services: from an iPhone, send a spoken request for a small change to a scratch GitHub repository. The real Claude Code and Codex CLIs run, a PR opens, and the check-in arrives as a phone notification from Telegram.

## Build order

| Order | Deliver |
| --- | --- |
| 1 | Telegram polling, allowlisted user, update log, outbox, transcript echo |
| 2 | Decider turn with tools; project grant buttons; auto-approval under grant |
| 3 | Podman agent image and egress proxy; container-based `restricted_run` and reconciliation |
| 4 | Claude Code implementer, check-failure revision, Codex adversary |
| 5 | Server-side publication to GitHub; removal of Web Push, Ollama reviewer, and static project registration |

## Remaining choices and deferrals

Before implementation:

- Pin the decider model, the Claude Code and Codex CLI versions and headless flags, the transcription model, and the proxy allowlist.
- Claude Max supports headless use through `setup-token`. Its 5-hour and weekly limits are shared with the user's interactive use. Confirm Codex's ChatGPT-login equivalent for headless use. At low volume, subscription limits are acceptable; API keys through the same CLIs remain the fallback.

Deferred to slice 5, the voice polish: spoken replies from a local TTS model on the GPU box, moving transcription to the GPU, and a live conversational voice mode.

Also deferred: implementer questions in the middle of a Run, provider fallback, parallel Runs across projects, more workers, merge automation, tracking PRs after they open, monorepo or multi-repository Runs, and group chats.

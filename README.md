# Teem

Teem is a hands-off, voice-driven coding system for one user. You talk to a Telegram bot. A model-backed decider turns your request into a bounded Run on a GitHub repository. Claude Code implements it, objective checks run, and Codex reviews the exact result adversarially. Failing checks or a changes-required review trigger up to two revisions. A passing Candidate is pushed to a `teem/<run>` branch and opened as a pull request, and the bot sends you the link. You merge on GitHub.

Work starts only after you tap **Approve** on a proposal, or under a standing project grant you created with an **Allow** button. Nothing you say or type counts as approval. See [v0](docs/architecture/v0.md) for the architecture and [slice 4](docs/architecture/vertical-slice-4.md) for the current design.

## Components

| Where | What |
| --- | --- |
| Server (TrueNAS) | `teem-server`: PostgreSQL state, controller, artifact store, Telegram bot, decider, local voice transcription, GitHub publisher, and authenticated evidence pages |
| Worker (development machine) | `teem-worker`: claims Attempts over outbound HTTPS and runs the implementer, checks, and reviewer in egress-restricted rootless Podman containers |

## Server setup

Requirements: Python 3.11+, PostgreSQL, Git, and HTTPS termination in front of the loopback server. Voice notes also need FFmpeg/ffprobe, Bubblewrap, and a static `whisper-cli` from [whisper.cpp v1.9.4](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.4). See [TrueNAS deployment](deploy/truenas/README.md) for a complete example.

Install with `uv sync` or `pip install .`. Initialize a fresh database with `teem-server init --dsn "$TEEM_DSN"`. Initialization is not a migration tool: apply [slice-3.sql](docs/architecture/slice-3.sql) to a slice-2 database and [slice-4.sql](docs/architecture/slice-4.sql) to a slice-3 database, each exactly once.

The server reads these configuration files:

- **GitHub** (required): the owners Teem may work under, and a fine-grained token with read/write access to contents and pull requests on those repositories. Only the server holds this token. Server code pushes only `teem/*` branches, and agents never see it.

  ```json
  {"owners": ["your-github-user"], "token": "<fine-grained token>"}
  ```

- **Decider**: an OpenRouter key and the exact model slug. The decider sees the recent conversation, project names, and Run status summaries, but no source code. Without this file, chat messages are acknowledged but cannot start work.

  ```json
  {"api_key": "<OpenRouter key>", "model": "anthropic/claude-sonnet-5", "timeout": 60}
  ```

- **Telegram**: the bot token from @BotFather and your numeric user ID. The server long-polls the Bot API, so there is no webhook or inbound port, and it accepts only your private chat.

  ```json
  {"token": "<bot token>", "user_id": 123456789}
  ```

- **Reviewer**: the identity, instructions, data destination, and timeout (up to 30 minutes) that every contract records:

  ```json
  {"identity": "openai-codex", "instructions": "Review the Candidate strictly against its original acceptance criteria.", "destination": "openai", "timeout": 1200}
  ```

- **Speech** (optional, for voice notes): a hash-pinned `whisper-cli` and model. The server verifies both hashes at startup, runs them in a Bubblewrap sandbox, and deletes audio after transcription. `ggml-base.en.bin` is fast but inaccurate, so measure a larger English model against the 120-second clip and 180-second inference bounds on your CPU.

  ```json
  {"executable": "/srv/teem/bin/whisper-cli", "executable_sha256": "<sha256>", "model": "/srv/teem/models/ggml-small.en.bin", "model_sha256": "<sha256>", "language": "en"}
  ```

```sh
teem-server serve --dsn "$TEEM_DSN" --artifacts /srv/teem/artifacts --username "$TEEM_USER" --password "$TEEM_PASSWORD" \
  --worker-id worker-1 --worker-token "$TEEM_WORKER_TOKEN" --origin https://teem.example \
  --reviewer-config reviewer.json --github-config github.json --decider-config decider.json --telegram-config telegram.json \
  --speech-config speech.json --speech-scratch /srv/teem/speech-scratch
```

The server binds to `127.0.0.1:8765`. The HTTPS proxy must forward `Authorization` and `Origin`. Evidence pages use a sign-in form (so password managers can fill it) that sets a signed, HttpOnly session cookie for 30 days; changing the password signs every browser out. Basic authentication still works for scripts. Every browser POST must be same-origin. Workers use a separate bearer token and protocol version 3. The server needs outbound HTTPS to `api.telegram.org`, `openrouter.ai`, `github.com`, and `api.github.com`. It keeps rebuildable repository mirrors under `<artifacts>/mirrors`.

## Worker setup

Follow [worker setup](deploy/worker/README.md) to build the agent and proxy images and to create the internal network and allowlisting proxy. Then install `teem/reviewer_codex.py` as an executable outside any repository, such as `/srv/teem/bin/reviewer-codex`, and write the worker policy:

```json
{
  "owners": ["your-github-user"],
  "token": "<read-only token for private repositories, optional>",
  "image": "localhost/teem-agent:1",
  "network": "teem-agents",
  "proxy": "http://10.203.7.2:8888",
  "coder": {"argv": ["teem-implement"], "env": {"CLAUDE_CODE_OAUTH_TOKEN": "<from claude setup-token>"}},
  "reviewer": {
    "identity": "openai-codex",
    "instructions_sha256": "<SHA-256 of the exact reviewer instructions>",
    "destination": "openai",
    "timeout": 1200,
    "executable": "/srv/teem/bin/reviewer-codex",
    "home": "/srv/teem/codex-home"
  }
}
```

To review with Claude instead of Codex, install `teem/reviewer_claude.py` as the reviewer executable, give the reviewer role `CLAUDE_CODE_OAUTH_TOKEN` in its `env`, and use `"destination": "anthropic"` (and a matching identity, such as `claude-reviewer`) in both `reviewer.json` and the worker policy. Each review is still a fresh session that never sees the implementer's conversation, but the same model family writing and reviewing catches fewer shared blind spots. Create the Claude token with `claude setup-token`. It uses your Claude subscription and shares its usage limits with your interactive use. Log Codex in once with `HOME=/srv/teem/codex-home codex login --device-auth`. The reviewer mounts that directory as its home so Codex can refresh its token. Each role's `env` and `home` reach only that role's containers. Set `TEEM_CLAUDE_MODEL` or `TEEM_CODEX_MODEL` in a role's `env` to choose each role's default model. A request can also ask for the implementer model for one Run, such as "use Opus for this"; the contract records `sonnet` or `opus`, the Approve message shows it, and it overrides the worker default for that Run.

```sh
teem-worker --url https://teem.example --token-file /srv/teem/config/worker-token --worker-id worker-1 --projects policy.json --state-dir /srv/teem/worker-state
```

## How a Run works

1. **Proposal.** You send a voice note or text. Voice is transcribed on the server and echoed back ("Heard: …"). The decider replies, asks a clarifying question, or proposes a Run. A proposal names the repository, objective, and acceptance criteria.
2. **Authorization.** On a granted repository, the Run starts at once. Otherwise the bot sends the exact scope (criteria, checks, base commit, revision limit) with Approve/Deny buttons. Buttons are composed from current state when sent, and stale taps are rejected. Say that Teem may always work on a repository to get an Allow prompt. Typed `/revoke owner/name` removes the grant; commands are never taken from transcripts.
3. **Execution.** Each Run's base is the default-branch head when it is proposed. Its checks come from `.teem/checks.json` at that commit, for example `[{"name": "unit tests", "argv": ["python3", "-m", "unittest"]}]`. A repository without that file gets no objective checks, and the proposal says so.
4. **Implementation.** `teem-implement`, baked into the agent image, prompts Claude Code with the contract, plus the review findings, reproduction tests, and failed check output on revisions. If it makes no changes, the Run stops and relays its question to you.
5. **Review.** `reviewer_codex.py` runs Codex against a disposable checkout of the exact Candidate. Its context holds the objective, criteria, diff, check evidence, and an index of files. It never includes the implementer's conversation or summary. A finding may attach one reproduction test.
6. **Delivery.** A passing review makes the Candidate ready. The server pushes the preserved Candidate itself to `teem/<run>` and opens one pull request. Repeating publication after a crash cannot create a second PR. A revoked grant stops publication.

Limits:

- **Run:** three-hour deadline from approval, eight Attempts across the Run, two Attempts per Task, and zero to two revisions. A revision is consumed by either a changes-required review or failing checks.
- **Subprocesses:** 45 minutes for the implementer, 10 minutes per check, and the configured reviewer timeout. Each is also clamped to the remaining Run deadline.
- **Sizes:** candidate bundles up to 50 MiB, review diffs up to 512 KiB, and review contexts up to 1 MiB.

A lost lease stays unresolved until worker reconciliation confirms the old container stopped.

When a Run stops, the check-in says why:

- **A failure** shows its last error, such as a provider's 401.
- **A reviewer failure** offers **Retry review**, which reviews the same Candidate again without redoing the implementation. Each Run gets one retry, adding two review attempts.
- **A Run stopped at review** (revision limit or an uncertain reviewer) shows the review summary and top findings, and offers **Publish anyway**. That opens the pull request titled "review not passed", with the open findings in its description. Both buttons are recorded as Approvals.

If queued work waits 10 minutes while the worker has been silent for five, the bot says so once per Run.

## Tests

The acceptance suite uses a disposable PostgreSQL admin connection and rootless Podman. It builds test-tagged agent and proxy images from `deploy/worker` when they are missing, and creates a temporary internal network and proxy per test class. The agent tests run the real `teem-implement` and `reviewer_codex.py` in the agent image, with fake `claude` and `codex` executables on `PATH`, so no credentials are needed. Telegram, OpenRouter, and the GitHub API are fake local endpoints. Git pushes go to a local repository directory.

```sh
TEEM_TEST_DSN=postgresql://postgres:password@127.0.0.1:5432/postgres python -m unittest -v tests.test_slice tests.test_slice4
```

Before relying on it, run the manual acceptance in [slice 4](docs/architecture/vertical-slice-4.md#acceptance) once with real services: a spoken request from your phone, real Claude Code and Codex, a pull request on a scratch repository, and the check-in arriving as a Telegram notification.

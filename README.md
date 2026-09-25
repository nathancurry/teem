# Teem (slice 4 in progress)

Teem runs one authorized coding Task on a GitHub repository, objective checks, and an independent read-only review. A valid review can request up to two bounded revisions. Passing checks and a fresh passing review of the preserved Candidate produce **Ready to merge**. A Telegram bot is the main interface: voice notes and text go to a model-backed decider, which proposes Runs through validated tools. Work starts only after an Approve button tap or under a standing project grant the user created with an Allow button. The phone PWA remains for evidence pages. Merge and deployment remain outside Teem. See [slice 4](docs/architecture/vertical-slice-4.md); steps 1–4 (Telegram channel, decider, grants, container sandbox, Claude Code implementer, Codex adversary) are implemented; pull request publication is not yet.

## Requirements

- Python 3.11+ and PostgreSQL on the server. Python 3.11+, Git, and rootless Podman on the worker; see [worker setup](deploy/worker/README.md) for the agent image, internal network, and allowlisting proxy.
- Projects are GitHub repositories under configured owners. The server and worker each keep their own mirror clone. Coder and check commands run in isolated workspaces. The reviewer executable must be installed on the worker.
- Durable server artifact storage and worker state storage. Back up artifacts with PostgreSQL. HTTPS termination in front of the loopback server is required for worker connections.
- FFmpeg/ffprobe, Bubblewrap, and a static `whisper-cli` built from [whisper.cpp v1.9.4](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.4). Install the [`ggml-base.en.bin` model](https://github.com/ggml-org/whisper.cpp#quick-start) outside the repository. Its SHA-256 is `a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002`. The server verifies both the model and runner executable hashes at startup. A static binary avoids mounting extra runner libraries into the speech sandbox.
- One stable VAPID private key outside the repository and a `mailto:` contact. The Web Push library is pinned to `pywebpush==2.5.0`.

Install with `uv sync` or `pip install .`. Initialize a fresh development database with `teem-server init --dsn "$TEEM_DSN"`. Existing slice-1 databases need explicit inspection and SQL alteration before use; initialization does not migrate them.

For an existing slice-2 database, apply [slice-3.sql](docs/architecture/slice-3.sql) once with a PostgreSQL client before starting the new server. This adds only subscriptions and notification deliveries; historical events are not backfilled.

Create a private speech scratch directory outside artifact storage and backups. Its abandoned `teem-speech-*` directories are removed when the server starts. Supply a configuration file like this, replacing paths and the executable hash with the installed binary's SHA-256:

```json
{
  "executable": "/srv/teem/bin/whisper-cli",
  "executable_sha256": "<sha256 of installed static whisper-cli>",
  "model": "/srv/teem/models/ggml-base.en.bin",
  "model_sha256": "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
  "language": "en"
}
```

Generate and retain one VAPID key. Keep it out of the repository and back it up with server secrets:

```sh
python -c 'from py_vapid import Vapid02; v=Vapid02(); v.generate_keys(); v.save_key("/srv/teem/secrets/vapid.pem")'
```

Projects are created from Telegram when the decider proposes work on a GitHub `owner/name`. Each Run's base is the default-branch head when it is proposed. Its objective checks come from `.teem/checks.json` at that commit, for example:

```json
[{"name":"unit tests","argv":["/usr/bin/python3","-m","unittest","discover"]}]
```

A repository without that file gets Runs with no objective checks, and the proposal says so. The server's GitHub configuration lists the owners Teem may work under and an optional token for private repositories:

```json
{"owners": ["your-github-user"], "token": "<fine-grained token with contents read>"}
```

The server keeps its mirrors under `<artifacts>/mirrors`; they are a rebuildable cache. The decider configuration names an OpenRouter model:

```json
{"api_key": "<OpenRouter key>", "model": "anthropic/claude-sonnet-5", "timeout": 60}
```

Use the model's exact OpenRouter slug. The decider receives the recent conversation, project names, and Run status summaries, but no source code.

Without a decider configuration, chat messages are acknowledged but cannot start work.

The implementer is Claude Code and the adversarial reviewer is Codex, each running headless in the worker's agent image. The server reviewer configuration fixes the reviewer identity, instructions, data destination, and timeout:

```json
{"identity":"openai-codex","instructions":"Review the Candidate strictly against its original acceptance criteria.","destination":"openai","timeout":1200}
```

On the worker, install `teem/reviewer_codex.py` as an executable outside any repository, such as `/srv/teem/bin/reviewer-codex`. The worker copies it into each review container. There it reads `/context.json`, runs `codex exec` against a disposable checkout of the exact Candidate at `/workspace`, and prints one JSON judgment. The review context carries the objective, criteria, diff, check evidence, and a line-count index of the Candidate's text files. The implementer's conversation and summary are never included. A finding may attach one reproduction test, which the next revision receives. The implementer, `teem-implement`, is baked into the agent image. It builds its prompt from `/contract.json`, including review findings and failed check output on revisions, and prints Claude Code's final summary. If it makes no changes, the Run stops as blocked with that output as a question for the user. Failing checks queue a revision within the Run's revision limit.

The worker policy file lists the GitHub owners the worker accepts, the agent image, network, and proxy (see [worker setup](deploy/worker/README.md)), and the coder and reviewer roles. Each role's `env` and optional `home` are passed only into that role's containers. Create the Claude token with `claude setup-token`. Log Codex in once with `HOME=/srv/teem/codex-home codex login --device-auth`; the reviewer mounts that directory as its home so Codex can refresh its token. `instructions_sha256` is the SHA-256 of the exact UTF-8 reviewer instructions. The worker verifies that each contract's checks match `.teem/checks.json` at the contract's base in its own mirror.

```json
{
  "owners": ["your-github-user"],
  "token": "<optional read-only token for private repositories>",
  "image": "localhost/teem-agent:1",
  "network": "teem-agents",
  "proxy": "http://10.203.7.2:8888",
  "coder": {"argv": ["teem-implement"], "env": {"CLAUDE_CODE_OAUTH_TOKEN": "<from claude setup-token>"}},
  "reviewer": {
    "identity": "openai-codex",
    "instructions_sha256": "<SHA-256 of exact instructions>",
    "destination": "openai",
    "timeout": 1200,
    "executable": "/srv/teem/bin/reviewer-codex",
    "home": "/srv/teem/codex-home"
  }
}
```

Set `TEEM_CLAUDE_MODEL` or `TEEM_CODEX_MODEL` in a role's `env` to pin a model.

Start one server and one outbound worker:

```sh
teem-server serve --dsn "$TEEM_DSN" --artifacts /srv/teem/artifacts --username "$TEEM_USER" --password "$TEEM_PASSWORD" --worker-id worker-1 --worker-token "$TEEM_WORKER_TOKEN" --origin https://teem.example --reviewer-config reviewer.json --speech-config speech.json --speech-scratch /srv/teem/speech-scratch --vapid-private-key /srv/teem/secrets/vapid.pem --vapid-subject mailto:operator@example.com --github-config github.json --decider-config decider.json --telegram-config telegram.json
teem-worker --url https://teem.example --token "$TEEM_WORKER_TOKEN" --worker-id worker-1 --projects projects.json --state-dir /srv/teem/worker-state
```

The server binds to `127.0.0.1:8765` by default. The proxy must forward `Authorization` and `Origin`. Browser decisions require Basic authentication and same-origin POSTs; workers use a separate bearer credential and protocol version 3.

The phone app must be served at the HTTPS origin root so `/sw.js` can control it. Allow authenticated 8 MiB audio uploads and 30-second upload timeouts at the proxy. Keep authenticated pages and API responses uncached. Allow outbound HTTPS Web Push to `fcm.googleapis.com`, `updates.push.services.mozilla.com`, and `*.push.apple.com`; the application rejects redirects and private/local push destinations. Confirm the host can transcribe a 120-second clip within the 180-second decode/inference bound and 2 GiB process limit before enabling dictation. The service receives raw audio only until local transcription completes. Submitted text retains the existing Run lifetime and downstream project routing policy.

Limits are a three-hour Run deadline from approval, eight Attempts across the Run, two Attempts per Task, zero to two revisions (each consumed by either a changes-required review or failing checks), a 45-minute coder timeout, a 10-minute timeout per check, and the configured reviewer timeout of up to 30 minutes. Each subprocess is also clamped to the remaining Run deadline. Candidate bundles are capped at 50 MiB, the review diff at 512 KiB, and the whole review context at 1 MiB. A lost lease remains unresolved until worker reconciliation confirms stop.

Telegram: pass `--telegram-config telegram.json` containing `{"token": "<bot token>", "user_id": <numeric user id>}`. The server long-polls the Bot API, stores each accepted update before confirming it, echoes transcribed voice notes, and passes the text to the decider. Decisions arrive as Approve/Deny and Allow/Deny buttons composed from current state, so a late message never offers a decision already made. Typed `/revoke owner/name` removes a standing grant; commands are never taken from transcripts. Updates from any other user or chat are recorded only as ignored IDs. Apply [slice-4.sql](docs/architecture/slice-4.sql) once to an existing slice-3 database. Voice notes use the same local runner, now bounded to 120-second clips and 180 seconds of decode and inference; confirm that bound on the server CPU with the chosen model.

Run the acceptance suite with a disposable database admin connection. It needs rootless Podman; it builds `localhost/teem-agent:dev` and `localhost/teem-proxy:dev` if missing (override the agent image with `TEEM_TEST_AGENT_IMAGE`) and creates a temporary internal network and proxy per test class. The server's speech runner still uses Bubblewrap:

```sh
TEEM_TEST_DSN=postgresql://postgres:password@127.0.0.1:5432/postgres python -m unittest -v tests.test_slice
TEEM_TEST_DSN=postgresql://postgres:password@127.0.0.1:5432/postgres python -m unittest -v tests.test_slice3
TEEM_TEST_DSN=postgresql://postgres:password@127.0.0.1:5432/postgres python -m unittest -v tests.test_slice4
```

The agent tests run the real `teem-implement` and `reviewer_codex.py` in the agent image, with fake `claude` and `codex` executables on `PATH`; no credentials are needed.

Set `TEEM_REAL_WHISPER_BIN`, `TEEM_REAL_WHISPER_MODEL`, and `TEEM_REAL_SPEECH_WAV` to run the speech acceptance path with the installed `whisper-cli`, model, and a speech WAV. The default deterministic acceptance fixture still runs the real FFmpeg/Bubblewrap pipeline. The notification scenario uses real Web Push encryption against a controllable local HTTP endpoint; production subscription validation accepts only the HTTPS destinations above.

Before phone acceptance, record browser and OS versions on one installed iPhone Home Screen app and one installed Android PWA. On each, check relaunch, Basic authentication, authenticated upload, actual recording format, track shutdown on interruption, background and closed-app push, notification navigation to current state, and ordinary Approve/Deny. An unsupported feature is a recorded fallback, not a passing device check. [WebKit documents](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/) that iPhone push requires an installed Home Screen app and a direct permission gesture.

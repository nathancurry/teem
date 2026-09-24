# Teem vertical slice 3

Teem runs one authorized coding Task, objective checks, and an independent read-only review. A valid review can request up to two bounded revisions. Passing checks and a fresh passing review of the preserved Candidate produce **Ready to merge**. The phone PWA adds server-local dictation and generic Web Push hints. Dictation and notification never authorize work. Merge and deployment remain outside this slice.

## Requirements

- Python 3.11+, PostgreSQL, Git, Bash, and Bubblewrap on the worker.
- A worker-owned local repository for each project. Coder and check commands run in isolated workspaces. The reviewer executable must be installed outside the repository.
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

Register a project with a checks JSON file, for example:

```json
[{"name":"unit tests","argv":["/usr/bin/python3","-m","unittest","discover"]}]
```

```sh
teem-server register-project --dsn "$TEEM_DSN" --id my-project --name 'My project' --base "$(git -C /srv/teem/clones/my-project rev-parse HEAD)" --checks checks.json
```

The server reviewer configuration fixes the runner identity, instructions, local Ollama destination, model, and timeout. The model must already be installed locally and fit the bounded context:

```json
{"identity":"local-ollama/qwen2.5-coder:14b-32k","instructions":"Review the supplied Candidate against its original acceptance criteria.","destination":"local-ollama","model":"qwen2.5-coder:14b-32k","timeout":600}
```

Install `teem/reviewer_ollama.py` as an executable outside the project repository, such as `/srv/teem/bin/reviewer-ollama`. The worker project file maps the project ID to locally controlled paths and commands. `instructions_sha256` is the SHA-256 of the exact UTF-8 instructions above. The reviewer executable reads `/context.json` and writes one JSON judgment to standard output. It runs with a fresh read-only context, disposable scratch, no repository mount, and no network. A one-use Unix socket in that scratch connects only to the worker's fixed `127.0.0.1:11434` Ollama endpoint; the worker supplies the frozen model and context.

```json
{
  "my-project": {
    "repo": "/srv/teem/clones/my-project",
    "coder": ["/usr/bin/python3", "/workspace/coder.py"],
    "checks": [{"name":"unit tests","argv":["/usr/bin/python3","-m","unittest","discover"]}],
    "reviewer": {
      "identity": "local-ollama/qwen2.5-coder:14b-32k",
      "instructions_sha256": "<SHA-256 of exact instructions>",
      "destination": "local-ollama",
      "model": "qwen2.5-coder:14b-32k",
      "timeout": 600,
      "executable": "/srv/teem/bin/reviewer-ollama"
    }
  }
}
```

Start one server and one outbound worker:

```sh
teem-server serve --dsn "$TEEM_DSN" --artifacts /srv/teem/artifacts --username "$TEEM_USER" --password "$TEEM_PASSWORD" --worker-id worker-1 --worker-token "$TEEM_WORKER_TOKEN" --origin https://teem.example --reviewer-config reviewer.json --speech-config speech.json --speech-scratch /srv/teem/speech-scratch --vapid-private-key /srv/teem/secrets/vapid.pem --vapid-subject mailto:operator@example.com
teem-worker --url https://teem.example --token "$TEEM_WORKER_TOKEN" --worker-id worker-1 --projects projects.json --state-dir /srv/teem/worker-state
```

The server binds to `127.0.0.1:8765` by default. The proxy must forward `Authorization` and `Origin`. Browser decisions require Basic authentication and same-origin POSTs; workers use a separate bearer credential and protocol version 2.

The phone app must be served at the HTTPS origin root so `/sw.js` can control it. Allow authenticated 8 MiB audio uploads and 30-second upload timeouts at the proxy. Keep authenticated pages and API responses uncached. Allow outbound HTTPS Web Push to `fcm.googleapis.com`, `updates.push.services.mozilla.com`, and `*.push.apple.com`; the application rejects redirects and private/local push destinations. Confirm the host can transcribe a 60-second clip within the 90-second decode/inference bound and 2 GiB process limit before enabling dictation. The service receives raw audio only until local transcription completes. Submitted text retains the existing Run lifetime and downstream project routing policy.

Limits are a 30-minute Run deadline from approval, eight Attempts across the Run, two Attempts per Task, zero to two revisions, a 15-minute coder timeout, a five-minute timeout per check, and the configured finite reviewer timeout. Each subprocess is also clamped to the remaining Run deadline. Candidate bundles are capped at 50 MiB and review input at 24 KiB so it fits the configured 32K context. A lost lease remains unresolved until worker reconciliation confirms stop.

Run the PostgreSQL and Bubblewrap acceptance suite with a disposable database admin connection:

```sh
TEEM_TEST_DSN=postgresql://postgres:password@127.0.0.1:5432/postgres python -m unittest -v tests.test_slice
TEEM_TEST_DSN=postgresql://postgres:password@127.0.0.1:5432/postgres python -m unittest -v tests.test_slice3
```

Set `TEEM_OLLAMA_SMOKE_MODEL` to an installed local model to include the real reviewer smoke test.

Set `TEEM_REAL_WHISPER_BIN`, `TEEM_REAL_WHISPER_MODEL`, and `TEEM_REAL_SPEECH_WAV` to run the speech acceptance path with the installed `whisper-cli`, model, and a speech WAV. The default deterministic acceptance fixture still runs the real FFmpeg/Bubblewrap pipeline. The notification scenario uses real Web Push encryption against a controllable local HTTP endpoint; production subscription validation accepts only the HTTPS destinations above.

Before phone acceptance, record browser and OS versions on one installed iPhone Home Screen app and one installed Android PWA. On each, check relaunch, Basic authentication, authenticated upload, actual recording format, track shutdown on interruption, background and closed-app push, notification navigation to current state, and ordinary Approve/Deny. An unsupported feature is a recorded fallback, not a passing device check. [WebKit documents](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/) that iPhone push requires an installed Home Screen app and a direct permission gesture.

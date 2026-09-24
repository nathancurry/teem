# Teem vertical slice 2

Teem runs one authorized coding Task, objective checks, and an independent read-only review. A valid review can request up to two bounded revisions. Passing checks and a fresh passing review of the preserved Candidate produce **Ready to merge**. Merge and deployment are outside this slice.

## Requirements

- Python 3.11+, PostgreSQL, Git, Bash, and Bubblewrap on the worker.
- A worker-owned local repository for each project. Coder and check commands run in isolated workspaces. The reviewer executable must be installed outside the repository.
- Durable server artifact storage and worker state storage. Back up artifacts with PostgreSQL. HTTPS termination in front of the loopback server is required for worker connections.

Install with `uv sync` or `pip install .`. Initialize a fresh development database with `teem-server init --dsn "$TEEM_DSN"`. Existing slice-1 databases need explicit inspection and SQL alteration before use; initialization does not migrate them.

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
teem-server serve --dsn "$TEEM_DSN" --artifacts /srv/teem/artifacts --username "$TEEM_USER" --password "$TEEM_PASSWORD" --worker-id worker-1 --worker-token "$TEEM_WORKER_TOKEN" --origin https://teem.example --reviewer-config reviewer.json
teem-worker --url https://teem.example --token "$TEEM_WORKER_TOKEN" --worker-id worker-1 --projects projects.json --state-dir /srv/teem/worker-state
```

The server binds to `127.0.0.1:8765` by default. The proxy must forward `Authorization` and `Origin`. Browser decisions require Basic authentication and same-origin POSTs; workers use a separate bearer credential and protocol version 2.

Limits are a 30-minute Run deadline from approval, eight Attempts across the Run, two Attempts per Task, zero to two revisions, a 15-minute coder timeout, a five-minute timeout per check, and the configured finite reviewer timeout. Each subprocess is also clamped to the remaining Run deadline. Candidate bundles are capped at 50 MiB and review input at 24 KiB so it fits the configured 32K context. A lost lease remains unresolved until worker reconciliation confirms stop.

Run the PostgreSQL and Bubblewrap acceptance suite with a disposable database admin connection:

```sh
TEEM_TEST_DSN=postgresql://postgres:password@127.0.0.1:5432/postgres python -m unittest -v tests.test_slice
```

Set `TEEM_OLLAMA_SMOKE_MODEL` to an installed local model to include the real reviewer smoke test.

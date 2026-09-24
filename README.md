# Teem vertical slice 1

This implements the text workflow through a preserved Candidate with passing objective checks. The terminal success state is **Awaiting review**. It does not perform model review, merge, or deployment.

## Requirements

- Python 3.11 or newer, PostgreSQL, Git, Bash, and Bubblewrap on the worker.
- A worker-owned local clone for each project. The configured coder command must run without supervisor credentials or network access and must read `/contract.json` for the objective and acceptance criteria.
- A server directory on durable storage for Git bundles. Its parent, and the worker state directory's parent, must already exist on durable storage. Back up artifacts with PostgreSQL; artifact filenames are SHA-256 hashes and the server verifies bundles on download and before recording a Candidate.
- HTTPS termination in front of the loopback server. Use a trusted certificate. Worker connections require HTTPS.

Install with `uv sync` or `pip install .`.

Create a checks file, for example:

```json
[{"name":"unit tests","argv":["/usr/bin/python3","-m","unittest","discover"]}]
```

The worker project file maps a server project ID to a local clone and fixed commands:

```json
{
  "my-project": {
    "repo": "/srv/teem/clones/my-project",
    "coder": ["/usr/bin/python3", "/workspace/coder.py"],
    "checks": [{"name":"unit tests","argv":["/usr/bin/python3","-m","unittest","discover"]}]
  }
}
```

The coder in this example is a project-specific program. Its command and all check commands execute in a Bubblewrap sandbox with only the attempt workspace writable. The worker rejects assignments whose checks differ from local policy.

Initialize and register a project:

```sh
teem-server init --dsn "$TEEM_DSN"
teem-server register-project --dsn "$TEEM_DSN" --id my-project --name 'My project' --base "$(git -C /srv/teem/clones/my-project rev-parse HEAD)" --checks checks.json
```

Run one server and one outbound worker. Keep passwords and tokens outside source control and supply them through your service manager. The examples use environment variables for expansion:

```sh
teem-server serve --dsn "$TEEM_DSN" --artifacts /srv/teem/artifacts --username "$TEEM_USER" --password "$TEEM_PASSWORD" --worker-id worker-1 --worker-token "$TEEM_WORKER_TOKEN" --origin https://teem.example
teem-worker --url https://teem.example --token "$TEEM_WORKER_TOKEN" --worker-id worker-1 --projects projects.json --state-dir /srv/teem/worker-state
```

The server binds to `127.0.0.1:8765` by default. The proxy must pass the `Authorization` and `Origin` headers. The browser uses Basic authentication and a same-origin check for decisions. The worker uses a separate bearer credential and protocol version 1.

For this slice, limits are fixed: 45-second leases renewed every 10 seconds, a 30-minute Run deadline from approval, a 15-minute coder timeout, a five-minute timeout per check, a 2 GiB subprocess address-space limit, a 64-process limit inside the sandbox, and a 50 MiB artifact limit. Runs permit at most two Attempts. A lost lease stays uncertain until the worker reconnects; retry requires reconciliation.

To run the acceptance test, provide a PostgreSQL admin DSN. The test creates and drops a disposable database and runs a real Bubblewrap coder and check:

```sh
TEEM_TEST_DSN=postgresql://postgres:password@127.0.0.1:5432/postgres python -m unittest -v tests.test_slice
```

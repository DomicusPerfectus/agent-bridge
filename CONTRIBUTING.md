# Contributing

Agent Bridge 0.2.1 aims for small, provider-neutral, local-first components.
Contributions are under Apache License 2.0. GitHub Private Vulnerability Reporting
is the public-launch reporting policy; see SECURITY.md and the publication checklist.

## Development

Use Python 3.11+. Windows PowerShell, without activating the environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]" "setuptools>=68" wheel
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tools/generate_schema.py --check
.\.venv\Scripts\python.exe examples/local_demo.py --root .validation/demo
.\.venv\Scripts\python.exe examples/git_demo.py --root .validation/git-demo
```

Linux/macOS, with the environment activated:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]' "setuptools>=68" wheel
python -m unittest discover -s tests -v
python tools/generate_schema.py --check
python examples/local_demo.py --root .validation/demo
python examples/git_demo.py --root .validation/git-demo
```

The runtime has no dependencies. `jsonschema` is used only for protocol
conformance tests. Tests create synthetic temporary projects under the ignored
`.validation/tests/` directory and exercise both the library and separate CLI
processes. CI runs the same suite on Windows and Linux with Python 3.11 and 3.14.

Git tests use real temporary local bare repositories and clones. They never need
GitHub credentials or internet access. The Git demo requires a new empty scratch
directory and does not overwrite an existing demo project. Git must be on PATH;
the local-only runtime continues to work without Git.

Recovery tests use real reference-lock rejections and a trusted, test-only Git
alias to stall a subprocess tree. Failure injection is limited to deterministic
I/O denial, caller interruption during a wait and interruption between receives.
No message content becomes a command.
Ubuntu is prepared in CI; Linux PASS requires a completed hosted Ubuntu run.

For an offline development environment with packaging tools and test dependencies
already installed, add `--no-build-isolation --no-deps` to pip's install command.

## Change guidelines

- Keep protocol, storage, transport, adapters and CLI separate. Put shared
  lifecycle rules in the protocol/application layer, not in individual adapters.
- Add meaningful tests for new lifecycle behavior, interoperability or failure
  modes. Include a process-level flow when changing CLI behavior.
- Preserve idempotent delivery and validated causal ordering. Do not modify
  published envelopes or auto-execute incoming instructions.
- Document changes to payloads, statuses and extension contracts. Unknown versions
  must fail clearly. Regenerate `schema.json` with your platform's venv Python
  and `tools/generate_schema.py` when its source changes.
- Use synthetic examples. Never commit bridge runtime state, credentials, raw
  conversation histories or private project information.
- Keep provider-specific logic behind adapter interfaces. No paid service should
  be required for the core workflow.

## Before submitting

Run all tests, the schema check and the documented local demo. Inspect
`git diff --check` and your staged files. Include the concrete problem, resulting
behavior and validation in a contribution description. Publish no tokens or
private logs in issues or PRs.

The wheel must include the protocol JSON Schema, LICENSE and NOTICE. To verify
packaging after committing the change, run:

Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe tools/verify_fresh_clone.py
.\.venv\Scripts\agentbridge.exe --version
```

Linux/macOS:

```sh
python tools/verify_fresh_clone.py
agentbridge --version
```

The fresh-clone tool verifies an
offline local clone, wheel build/install, isolated runtime, CLI initialization
and the documented demo. It uses a temporary clone under `.validation/` and
also checks the Git E2E demo from the installed wheel. It
records a compact result there. Run it with a development Python that already
has pip and setuptools. The fresh runtime has no inherited site packages.

Before public distribution, complete [the publication checklist](docs/publication-checklist.md).

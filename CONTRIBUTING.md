# Contributing

Agent Bridge v0.1 aims for small, provider-neutral, local-first components.
Before public collaboration starts, the maintainer must select a license and
security contact. The existing empty LICENSE file is a placeholder.

## Development

Use Python 3.11+. Create a project-local virtual environment, activate it or use
its Python executable directly, then install:

```sh
python -m pip install -e '.[test]'
python -m unittest discover -s tests -v
python tools/generate_schema.py --check
python examples/local_demo.py --root .validation/demo
```

The runtime has no dependencies. `jsonschema` is used only for protocol
conformance tests. Tests create synthetic temporary projects under the ignored
`.validation/tests/` directory and exercise both the library and separate CLI
processes. CI runs the same suite on Windows and Linux with Python 3.11 and 3.14.

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
  must fail clearly. Regenerate `schema.json` with `python tools/generate_schema.py`
  when its source changes.
- Use synthetic examples. Never commit bridge runtime state, credentials, raw
  conversation histories or private project information.
- Keep provider-specific logic behind adapter interfaces. No paid service should
  be required for the core workflow.

## Before submitting

Run all tests, the schema check and the documented local demo. Inspect
`git diff --check` and your staged files. Include the concrete problem, resulting
behavior and validation in a contribution description. Publish no tokens or
private logs in issues or PRs.

To verify packaging, build a wheel with `python -m pip wheel --no-deps --wheel-dir
dist .`, install it into a fresh environment, and run `agentbridge --version`
and the demo. The wheel must include the protocol JSON Schema.

After committing a change, `python tools/verify_fresh_clone.py` verifies an
offline local clone, wheel build/install, isolated runtime, CLI initialization
and the documented demo. It uses a temporary clone under `.validation/` and
records a compact result there. Run it with a development Python that already
has pip and setuptools. The fresh runtime has no inherited site packages.

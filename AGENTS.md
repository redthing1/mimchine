# AGENTS.md

Python 3.11+ CLI for named OCI development environments using Podman or Docker.

- In `mimchine/`, keep CLI handling in `cli.py`, orchestration in `services.py`,
  models in `domain.py`, and backends in `engines/` and `builders/`.
- Route engine commands through `ProcessRunner`. Use fakes in `tests/support.py`;
  default tests must not invoke live engines. Live gVisor tests are opt-in; see
  `tests/test_gvisor_live.py` for prerequisites.
- Preserve immutable container bindings, ownership and isolation checks, atomic
  state writes, and retryable cleanup. Creation `--plan` must invoke no engine
  and write no state.
- Keep tracked content concise, self-contained, and free of private context or
  machine-specific paths. Update `README.md` or `docs/` when public behavior changes.

Development commands:

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run mim --help
```

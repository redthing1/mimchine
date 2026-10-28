# Development

Mim requires Python 3.11+. From a repository checkout:

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run mim --help
```

Use `uv run mim` to run the working tree. To build the bundled development image:

```sh
uv run mim build mim-fed:dev -f demo/mim_fed.docker -C demo
```

## Code structure

CLI parsing lives in `mimchine/cli.py`, orchestration in `services.py`, and models
in `domain.py`. Engine and builder integrations live in `engines/` and `builders/`.
Run backend commands through `ProcessRunner`; use the fakes in `tests/support.py`
for unit tests.

## Live tests

Default tests use fake engines. Live tests are opt-in and create temporary,
uniquely named machines. Use a test environment with the required runtime and
images available.

### Native updates

Supply an already-pulled immutable Alpine image containing BusyBox `nc`:

```sh
MIM_UPDATE_LIVE=1 MIM_UPDATE_RUNNER=podman MIM_UPDATE_IMAGE=sha256:... \
  uv run pytest -q tests/test_updates_live.py
```

For Docker, set `MIM_UPDATE_RUNNER=docker` and select a disposable daemon with
`DOCKER_HOST`. The suite uses temporary Mim state and removes its owned
containers and private images. It covers preservation, mounts, port reachability,
resource changes, identity, interrupted operations and CLI usage.

### gVisor

Install the [runtime and host prerequisites](isolation.md#install-the-runtime).
The fixture image must contain Python 3, `/bin/sh`, core tools and `/var/tmp`:

```sh
MIM_GVISOR_LIVE=1 MIM_GVISOR_RUNTIME=/usr/lib/mim-gvisor/runsc \
MIM_GVISOR_IMAGE=sha256:... uv run pytest -q tests/test_gvisor_live.py
```

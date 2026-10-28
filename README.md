# mimchine

**Persistent development sandboxes.**

A Linux userspace of your choice, with your projects mounted and your tools,
files, and shell history kept between sessions.

## Use

Build from your project's `Containerfile`, then enter the sandbox:

```sh
mim build app:dev -f Containerfile
mim create dev --image app:dev --workspace .
mim enter dev
```

Your project files stay on the host; `--workspace .` shares them with the sandbox.
Return with `mim enter dev`, or stop it between sessions with `mim stop dev`.

## Install

Requires Python 3.11+ and a container engine: Podman on Linux, or Docker on Linux
and macOS. Install from a checkout of this repository with uv:

```sh
uv tool install .
```

Podman is the default. For Docker, use `--builder docker` when building and
`--runner docker` when creating a machine, or save them as
[defaults](docs/machines.md#save-settings-in-a-profile).

## Guides

- [Working with machines](docs/machines.md) — shared files, ports, profiles and SSH.
- [Changing settings](docs/updates.md) — mounts, ports and resource limits after creation.
- [gVisor isolation](docs/isolation.md) — optional isolation with rootless Podman on Linux.
- [Development](docs/development.md) — working on Mim and running tests.

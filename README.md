# mimchine

ergonomic local **mini-machines**. build oci images, then create named machines with useful stuff mounted.

run using docker, podman, or kvm microvm.

## dev

```sh
uv sync --locked
uv run mim --help
```

build demo image:
```sh
uv run mim build mim-fed:dev -f demo/mim_fed.docker -C demo
```

create and enter machine:
```sh
uv run mim create dev --image mim-fed:dev --workspace .
uv run mim enter dev
```

run command:
```sh
uv run mim exec dev pwd
```

stop/delete it:
```sh
uv run mim stop dev
uv run mim delete dev -f
```

## backends

choose a builder:
```sh
mim build app:dev -f Containerfile -C . --builder podman
mim build app:dev -f Dockerfile -C . --builder docker
```

choose a runner:
```sh
mim create dev --image app:dev --runner podman
mim create dev --image app:dev --runner docker
mim create dev --image alpine --runner smolvm --net
```

# common options

```sh
mim create dev --image app:dev --workspace .
mim create dev --image app:dev --mount ./cache:/cache:ro
mim create web --image app:dev --port 8080:80 --net
mim create git --image app:dev --ssh-agent
mim create dev --image app:dev --host-user
mim create dev --image app:dev --root
```

## config

your config file is at `~/.config/mimchine/config.toml`

```toml
[defaults]
builder = "podman"
runner = "podman"
network = "default"
shell = "sh"

[profiles.work]
image = "mim-fed:dev"
workspace = "."
env = ["EDITOR=nvim"]
network = "none"
identity = "host"
shell = "zsh -l"
```

use a profile:
```sh
mim create work --profile work
mim enter work
```

## shell state

`mim enter` mounts per-machine shell state directory at `/mim/shell-state`. For `zsh` and `bash`, history is written there automatically. to keep shell state when deleting:
```sh
mim delete dev -f --keep-shell-state
```

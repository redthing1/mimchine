# mimchine

Ergonomic named development environments from OCI images. Uses Podman by
default; Docker is also supported.

## development

```sh
uv sync --locked
uv run mim --help
uv run mim build mim-fed:dev -f demo/mim_fed.docker -C demo
```

## use

```sh
mim create dev --image mim-fed:dev --workspace .
mim enter dev
mim exec dev pwd
mim stop dev
mim delete dev -f
```

Common creation options:

```sh
mim create dev --image app:dev --start
mim create dev --image app:dev --mount ./cache:/cache:ro
mim create web --image app:dev --port 8080:80 --net
mim create git --image app:dev --ssh-agent
mim create dev --image app:dev --host-user
mim create dev --image app:dev --runner docker
```

## gpu

```sh
mim setup gpu
mim create dev --image app:dev --gpu
```

With Podman on native Linux, `--gpu` exposes every detected GPU. NVIDIA
requires NVIDIA Container Toolkit with CDI. The image supplies its own Mesa,
ROCm, or CUDA userspace libraries.

## configuration

`~/.config/mimchine/config.toml`:

```toml
[defaults]
builder = "podman"
runner = "podman"
network = "default"
shell = "auto"
cpus = 2
memory = 2048

[profiles.work]
image = "mim-fed:dev"
workspace = "."
env = ["EDITOR=nvim"]
network = "none"
identity = "host"
```

```sh
mim create work --profile work
mim enter work
```

## ssh

```sh
mim setup ssh
ssh work.mim
mim ssh work
rsync -a ./src/ work.mim:/work/project/
mim setup ssh --remove
```

SSH uses the runner's exec transport: no guest daemon, port, or network is
required. Stopped machines start automatically. `rsync` must exist in the
image when used.

## shell state

`mim enter` stores Bash and Zsh history under `/mim/shell-state`. Preserve it
when deleting a machine with:

```sh
mim delete dev -f --keep-shell-state
```

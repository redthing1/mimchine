# Working with machines

A machine is a named environment created from an OCI image. Its filesystem
persists until you delete it; shared directories remain on the host.

## Create a machine

```sh
mim create dev --image app:dev --workspace .
mim enter dev
```

Use an existing image, or build one with `mim build app:dev -f Containerfile`.
Add `--runner docker` to use Docker, `--host-user` to run as your host user, or
`--env KEY=VALUE` to set an environment variable. `mim create --help` lists all
creation options.

`mim doctor` checks local prerequisites for your defaults. Use `--runner docker`
or `--profile work` to check a particular configuration. Rootless Podman needs
subordinate UID/GID ranges, `newuidmap`/`newgidmap`, and a networking helper such
as `pasta`. Doctor checks available tools; creation verifies that the engine
can actually run the machine. See [isolation](isolation.md) for gVisor setup.

## Share files

`--workspace .` shares the current directory at `/work/<directory-name>` and
sets the default session directory. Give it an explicit destination to keep the
same guest path across projects:

```sh
mim create dev --image app:dev --workspace .:/work/project
```

Use `--mount HOST:GUEST[:ro]` for other existing files or directories. Mounts
are writable by default; `:ro` makes them read-only. Host permissions still
apply. On SELinux hosts, shared data may need an explicit `:rw,z` or `:ro,z`.

`--home-share DIR` shares a directory beneath your host home at both its host
pathname and the corresponding path beneath `/home/user`. Each guest mount
destination must be unique.

## Publish a service

```sh
mim create web --image app:dev --port 8080:80
```

This publishes guest port 80 at `127.0.0.1:8080`. The service inside the machine
must listen on its network interface, not only guest loopback. To accept traffic
on all host IPv4 interfaces, use `--port 0.0.0.0:8080:80`. TCP is the default;
append `/udp` for UDP. `--no-net` disables guest networking.

## Save settings in a profile

Set defaults and reusable profiles in `~/.config/mimchine/config.toml`:

```toml
[defaults]
builder = "podman"
runner = "podman"

[profiles.work]
image = "app:dev"
workspace = ".:/work/project"
env = ["EDITOR=nvim"]
identity = "host"
cpus = 2
memory = 2048
```

```sh
mim create dev --profile work
```

Settings resolve from command line, profile, then defaults. Command-line mounts,
environment variables and port mappings override profile entries with the same
guest destination, variable name or host listener. Duplicate entries within one
layer are rejected. Profiles apply at creation; editing one does not change an
existing machine.

## Shells and SSH

`mim enter dev` opens a shell; `mim exec dev COMMAND` runs a command from the
host. Both start the machine if needed. Set `--shell zsh` or `--workdir /path`
at creation to choose session defaults. Bash and Zsh history is stored separately
from the container under `/mim/shell-state`.

To use SSH clients and tools such as rsync:

```sh
mim setup ssh
ssh dev.mim
rsync -a ./src/ dev.mim:/work/project/
```

The SSH transport uses engine exec, so it needs no guest SSH daemon, published
port or guest network. The destination image must provide `rsync` when used.
`mim ssh dev` connects directly; `mim setup ssh --remove` removes the client
integration. To use your host agent from inside a machine, create it with
`--ssh-agent`.

## Resources and GPUs

Set resource ceilings independently:

```sh
mim create dev --image app:dev --cpus 4 --memory 8192 --swap 0 --tasks 512
```

`--cpus` limits aggregate CPU time in CPU equivalents. `--memory` and `--swap`
are separate ceilings in MiB; zero swap disables swapping. `--tasks` limits host
processes and threads. Native engines require a memory ceiling when setting swap.
Omitted ceilings inherit host policy. These options also work in profiles and
`[defaults]` as `cpus`, `memory`, `swap` and `tasks`.

For per-command memory limits with native Podman, cgroup v2, systemd and crun:

```sh
mim create dev --image app:dev --job-limits
mim exec --memory 512 dev make
```

The command limit covers the process and its descendants.

GPU forwarding is available with native Podman on Linux:

```sh
mim setup gpu
mim create dev --image app:dev --gpu
```

`--gpu` exposes all detected GPUs. NVIDIA requires NVIDIA Container Toolkit with
CDI; the image supplies its own Mesa, ROCm or CUDA userspace libraries.

## Change settings

Use `mim update` to change mounts, ports, environment, session defaults or
resource ceilings. Preview with `--plan` before applying a change:

```sh
mim update dev --port 8080:80 --plan
mim update dev --port 8080:80 --restart
```

Some changes replace the container while preserving its files and interrupting
sessions. See [changing settings](updates.md) for replacement behavior and
supported configurations.

## Inspect, stop or delete

```sh
mim list
mim inspect dev
mim stop dev
```

`mim inspect` shows saved settings, the bound container and any problem preventing
execution. `mim start dev` starts it explicitly; enter, exec and SSH also start
stopped machines automatically.

`mim delete dev` removes the container, machine record and shell history. Shared
host files remain. Add `--keep-shell-state` to retain history, including when
retrying a deletion.

## Recover an interrupted operation

After addressing the error reported by Mim, run `mim recover dev`. It completes
an interrupted creation without starting the machine, or finishes an update
with its saved running intent. Use `mim delete dev` to discard an incomplete
creation or update. If inspection reports `deleting`, retry deletion.

Mim checks container ownership and the engine context before acting. A reused
container name does not become the machine's execution target, and recovery does
not recreate a missing bound container.

### Adopt an older machine record

If Mim reports that a record requires migration, generate an adoption plan:

```sh
mim migrate dev --plan > dev-adoption.json
```

Review the container ID, engine context, image, runtime, mounts, ports and limits,
then apply it with `mim migrate dev --apply dev-adoption.json`. Apply rechecks the
plan, saves the original record as `machine.schema-2.json`, and binds the existing
container without changing its running state or data. Changed inputs require a
new plan. Use one Mim version to write state during adoption and retain the
record backup until the handover is verified.

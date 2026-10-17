# Isolation

`native` uses the engine's ordinary container isolation. On Linux, `gvisor`
adds gVisor's userspace kernel through local rootless Podman.

```sh
mim create dev --image app:dev --isolation gvisor --workspace .
mim enter dev
mim exec dev make
mim stop dev
mim start dev
```

Existing machines retain their isolation mode. Build images as usual; isolation
is selected when creating a machine.

## Install the runtime

Install a versioned [Mim gVisor distribution](https://github.com/redthing1/gvisor),
including its sidecars, manifest, and fixed runtime profiles. The repository's
`mim/build packages` produces RPM and Debian packages using a containerized
build. Mim does not install runtimes or change host container configuration.

New machines select the newest release under `/usr/lib/mim-gvisor`. A custom
installation can be selected in `~/.config/mimchine/config.toml`:

```toml
[runtimes.gvisor]
path = "/opt/mim-gvisor/20261012.0-mim.3/runsc"

[profiles.isolated]
image = "app:dev"
isolation = "gvisor"
gpu = false
job_limits = false
```

```sh
mim create dev -P isolated
```

The path must be absolute. Each machine retains its canonical runtime version
and image ID. Installing a newer release changes selection for new machines;
keep older distributions installed while machines reference them.

The host needs cgroup v2 with delegated CPU, memory, swap, and task limits, a
working user systemd manager (254+), `busctl`, `systemd-run`, and pasta supporting
`--no-splice`.
The integration has been exercised with Podman 5.8.4 and Mim gVisor
20261012.0-mim.3 on x86-64 Linux.

## Resources and access

GVisor machines default to two CPUs and 4096 MiB. Override them with `--cpus`
and `--memory`, or the corresponding profile/default settings. The host budget
covers the sandbox, filesystem server, network helper, and concurrent exec
sessions. Swap is disabled. Mim verifies the effective boundary before starting
image code and before dispatching commands. Stop and delete also quiesce the
machine's owned host processes.

Writable rootfs changes, workspaces, shell history, stop/start, and interactive
shells retain the normal Mim workflow. Host-edited workspace files remain
visible; filesystem watchers may need polling. GPU forwarding, host networking,
raw container arguments, and per-command cgroup memory limits are excluded.
Guest setuid elevation is disabled.

Published ports default to `127.0.0.1`. Explicit IPv4/IPv6 addresses and TCP/UDP
are accepted through `--port`. Publishing requires networking; `--no-net`
disables it. Networking grants outbound access, without an egress allowlist.

Mounts cannot expose known Mim, Python, engine, runtime, or host-command control
paths. Writable workspaces and shared credentials still grant the authority
those paths provide. Disk capacity remains shared with the host.

`--ssh-agent` captures the selected host socket when the machine is created.
It enables host Unix-socket access: a read-only socket mount does not restrict
agent operations, and other sockets in shared directories can also be reached.
Mim's SSH exec transport works independently of guest networking.

## Inspect a plan

```sh
mim create dev --image app:dev --isolation gvisor --plan
```

`--plan` prints resolved settings, Podman command templates, and verification
requirements. It invokes no engine and writes no machine state. Placeholders
such as `<container-id>` remain unresolved; a plan does not establish host
compatibility. Isolation selection follows command line, profile, then defaults.

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

Install a [Mim gVisor distribution](https://github.com/redthing1/gvisor/releases),
including its sidecars, manifest, and fixed runtime profiles. The repository's
`mim/build packages` produces RPM and Debian packages using a containerized
build. Mim does not install runtimes or change host container configuration.

New machines use `/usr/lib/mim-gvisor/runsc`. Legacy versioned installations
remain discoverable when the stable package is absent. A custom
installation can be selected in `~/.config/mimchine/config.toml`:

```toml
[runtimes.gvisor]
path = "/opt/mim-gvisor/runsc"

[profiles.isolated]
image = "app:dev"
isolation = "gvisor"
gpu = false
job_limits = false
```

```sh
mim create dev -P isolated
```

The path must be absolute. Each machine binds its runtime manifest and image ID;
replacing a bound distribution is rejected. Retire affected Mim machines before
upgrading the package in place, or configure a retained archive installation for
machines that must outlive package upgrades. This binding is separate from
ordinary Compose services, which adopt package updates when recreated.

The host needs delegated cgroup v2, a working user systemd manager, `busctl`,
`systemd-run` (254+), and pasta supporting `--no-splice`. Configured resource
ceilings require their corresponding host controllers.
Use `mim doctor --isolation gvisor` to check local prerequisites; delegation and
effective isolation are verified when creating a machine.
The integration has been exercised with Podman 5.8.4 and Mim gVisor
20261012.0-mim.4 on x86-64 Linux.

## Resources and access

Resource ceilings are opt-in and independent of isolation. Use `--cpus`,
`--memory`, `--swap`, and `--tasks`, or the corresponding profile/default settings.
Unset ceilings inherit host policy; `--swap 0` disables swap. GVisor ceilings
cover the sandbox, filesystem server, network helper, and concurrent exec
sessions. The task ceiling counts host processes and threads, rather than guest
processes.

Mim verifies machine process ownership and every requested ceiling before
starting image code and dispatching commands. Stop and delete quiesce the
machine's owned host processes even when no ceilings are configured.

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

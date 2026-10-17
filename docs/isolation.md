# Isolation plans

`native` is the default isolation mode. The experimental `gvisor` mode compiles
a candidate policy for local rootless Podman on Linux. gVisor execution is not
enabled.

## Inspect a plan

```sh
mim create dev --image app:dev --plan
mim create dev --image app:dev --isolation gvisor --plan
```

`--plan` prints JSON with resolved machine settings, command templates, and
requirements. It invokes no engine and writes no machine state. Values such as
`<container-id>` and `<image-uid>` are unresolved placeholders. Plans describe
configuration; they do not establish host compatibility or authorize execution.

## Configure gVisor

```toml
[runtimes.gvisor]
path = "/usr/lib/mim-gvisor/20261012.0-mim.3/runsc"

[profiles.isolated]
image = "app:dev"
isolation = "gvisor"
gpu = false
job_limits = false
```

```sh
mim create dev -P isolated --plan
```

An explicit runtime path must be absolute. Runtime discovery selects the newest
release under `/usr/lib/mim-gvisor`; custom installations use the path above.
Mim expects an externally installed
[Mim gVisor distribution](https://github.com/redthing1/gvisor),
including its sidecars and fixed runtime profiles. The path need not exist to
inspect a plan. Isolation selection follows command line, profile, then defaults.
The selected runtime profile retains its settings across Podman invocations;
runtime flags cannot be overridden through mim configuration.

## Candidate policy

The policy selects systrap, sandbox networking, Gofer-mediated filesystem
access, shared bind-mount access, and a persistent writable rootfs managed by
Podman. Published ports default to `127.0.0.1`; explicit IPv4 or IPv6 addresses
and TCP or UDP can be supplied through `--port`.

GPU forwarding, host networking, raw container arguments, and the existing
per-command cgroup memory limiter are excluded. Guest setuid elevation is
disabled. Static mount checks protect known mim, engine, and runtime control
paths, with a narrow grant for the machine's shell history.

`--ssh-agent` enables host Unix-socket access. A read-only socket bind does not
restrict agent operations, and other sockets in shared directories may also be
reachable. Writable workspaces and network access retain the authority those
grants provide.

Execution requires verified runtime and policy identity, effective OCI settings,
and host CPU and memory enforcement covering the sandbox and its helpers before
the payload starts. These requirements are not yet implemented and qualified.
See gVisor's [documentation](https://gvisor.dev/docs/) for the runtime itself.

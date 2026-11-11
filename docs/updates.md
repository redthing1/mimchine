# Changing settings

`mim update` changes a machine's saved settings. Omitted settings keep their
current values; profiles are not reapplied.

## Preview and apply

```sh
mim update dev --mount ./data:/data:ro --port 8080:80 --plan
mim update dev --mount ./data:/data:ro --port 8080:80 --restart
```

`--plan` previews changes and replacement requirements without engine calls or
state writes. Environment values are redacted.

| Settings | Effect |
| --- | --- |
| Shell | Applies to future sessions |
| Resource ceilings | Updates in place; clearing a CPU or memory ceiling requires replacement |
| Mounts, ports, environment, working directory | Replaces the container |

A running replacement requires `--restart`; stopped machines stay stopped.
Native Podman and Docker support these updates; gVisor supports only shell
changes.

## Set or remove a value

```sh
mim update dev --remove-mount /data --remove-port 8080:80 --restart
mim update dev --env MODE=debug --unset-env OLD_MODE --restart
mim update dev --cpus 4 --memory 8192
mim update dev --shell zsh
```

A mount replaces the entry at its guest destination; removing it leaves host
files intact. Managed shell-history mounts cannot be edited. Ports are keyed by
host address, port and protocol; removal requires the exact mapping.
`--unset-env` removes an override, restoring the image default if present.

Use `unlimited` to clear a resource ceiling, or `--swap 0` to disable swap.
Lowering a memory ceiling can terminate workload processes.

## What replacement preserves

Replacement preserves filesystem contents, numeric ownership, modes, hard links
and symlinks. Bind-mounted data and shell history stay on the host. Processes,
connections and memory-backed data do not survive replacement. ACLs and extended
attributes are not guaranteed.

Podman snapshots changes using existing image layers. Docker copies the full
filesystem and needs temporary space for that copy. Mim removes obsolete
private images; shared layers remain while needed.

Replacement preserves GPU forwarding, command job limits and raw container
arguments. Use Mim options to change mounts, ports, environment and working
directory.

Replacement requires:

- Bind mounts with distinct guest destinations.
- No SSH-agent forwarding.
- Explicit port addresses. Remove or replace rules with omitted addresses in
  the same update.

Docker also requires an image without declared volumes, healthchecks, ONBUILD
instructions or multiline configuration values.

`mim update` cannot change the runner, image, identity or isolation mode.

## Recover an interrupted update

Fix the reported error, then run `mim recover dev` to resume the update.

Use `mim delete dev` to discard a machine with a pending update. Retry failed
deletion with the same command.

See [working with machines](machines.md) for creation and lifecycle commands.

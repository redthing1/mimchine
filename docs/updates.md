# Changing settings

`mim update` changes a machine's saved settings. Omitted settings keep their
current values; profiles are not reapplied.

## Preview and apply

```sh
mim update dev --mount ./data:/data:ro --port 8080:80 --plan
mim update dev --mount ./data:/data:ro --port 8080:80 --restart
```

The preview shows what will change and whether it needs a replacement. It runs
without engine calls or state writes, and redacts environment values. Applying
the update checks the container binding and engine readiness; the preview does
not reserve ports.

| Settings | Effect |
| --- | --- |
| Shell | Applies to future sessions |
| Resource ceilings | Updates in place; clearing a CPU or memory ceiling requires replacement |
| Mounts, ports, environment, working directory | Replaces the container |

A running replacement requires `--restart`, permitting interruption of sessions.
Stopped machines stay stopped. For an in-place change, `--restart` does not force
a restart. Native Podman and Docker support these updates; gVisor supports only
shell changes.

## Set or remove a value

```sh
mim update dev --remove-mount /data --remove-port 8080:80 --restart
mim update dev --env MODE=debug --unset-env OLD_MODE --restart
mim update dev --cpus 4 --memory 8192
mim update dev --shell zsh
```

A mount replaces the entry at its guest destination. Removing it keeps host
files; other aliases can still expose the same source. Internal shell-history
mounts cannot be edited. A port replaces the mapping for its host address, port
and protocol; removal requires the exact mapping. `--unset-env` removes a Mim
override, restoring the image default if present.

Use `unlimited` to clear a resource ceiling, or `--swap 0` to disable swap.
Lowering a memory ceiling can terminate workload processes.

## What replacement preserves

Replacement copies the stopped root filesystem into a private local image,
preserving file contents, numeric ownership, modes, hard links and symlinks.
Shared files and shell history stay on the host. Running processes, connections
and memory-backed data do not survive replacement. ACLs and extended attributes
are not guaranteed.

Allow temporary disk space for a full filesystem copy. Mim removes obsolete
private images after replacement and the active image on deletion.

Replacement has these configuration requirements:

- Mounts must be bind mounts declared through Mim. The machine cannot use raw
  container arguments, GPU forwarding, command job limits or SSH-agent forwarding.
- Its image must have no declared volumes, healthchecks, ONBUILD instructions
  or multiline configuration values.
- Port mappings must have explicit addresses. Remove or replace older mappings
  with an omitted address in the update.

Runner, image, identity and isolation changes are outside this command's scope.

## Recover an interrupted update

Address the reported error, then run `mim recover dev` to finish the update.
Execution and stop remain blocked until recovery finishes. Recovery completes
the requested change; it does not roll back application writes to shared mounts.

To discard the machine instead, use `mim delete dev`. This also removes pending
allocations and private images. Failed deletion is retryable with the same
command; recovery cannot resume an update once deletion has begun.

See [working with machines](machines.md) for creation and lifecycle commands.

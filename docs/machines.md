# Machine management

Mim binds each machine to a specific container and engine context. Operations
check that binding and use the full container ID. Reusing a deleted container's
name does not replace the machine's execution target.

Podman requires local Linux. Docker also supports macOS development.

## Check host prerequisites

```sh
mim doctor
mim doctor -P work
mim doctor --isolation gvisor
```

Doctor checks base prerequisites for the runner, isolation and network mode in
your defaults or selected profile. Override them with `--runner`, `--isolation`
and `--no-net`. Checks cover local tools, rootless ID configuration and networking
helpers. GVisor adds distribution integrity, user systemd and cgroup v2; Docker
checks client availability.

Rootless Podman needs `newuidmap`/`newgidmap`, subordinate UID/GID ranges and a
user-mode networking helper. Current defaults use `pasta`, usually supplied by
the `passt` package; older configurations may use `slirp4netns`. Package names
vary by distribution. Offline environments skip networking-helper checks.

Doctor checks standard helpers on PATH; custom Podman helper paths may need
manual confirmation. It does not initialize the engine, pull images, start
containers or change host configuration. Errors produce a nonzero exit status;
warnings need review. Container execution, cgroup delegation and network
connectivity require verification during actual use.

## Resource limits

New machines have no Mim-imposed resource ceilings unless configured. Set each
ceiling independently at creation:

```sh
mim create dev --image app:dev --cpus 4 --memory 8192
mim create isolated --image app:dev --isolation gvisor --swap 0 --tasks 512
```

`--cpus` limits aggregate CPU time in CPU equivalents. `--memory` and `--swap`
are separate ceilings in MiB; zero swap disables swapping. `--tasks` limits host
processes and threads. GVisor includes runtime and exec helpers in the machine's
aggregate limits. Native engines require `--memory` when setting `--swap`.

The config keys are `cpus`, `memory`, `swap`, and `tasks`, in `[defaults]` or a
profile. Each value resolves from command line, profile, then defaults. Unset
values inherit host policy. `mim inspect` shows the saved ceilings; existing
machines retain their resource settings.

## Inspect and control a machine

```sh
mim list
mim inspect dev
mim start dev
mim stop dev
mim delete dev
```

`enter`, `exec`, and SSH start stopped machines automatically. Inspection shows
the requested settings, container ID, bound and observed runtime, engine context,
and any problem preventing execution. A changed context or binding requires
reconciliation before payload execution; a missing container is not recreated
automatically.

Deletion removes the container, machine record, and shell history. To retain
history, use `mim delete dev --keep-shell-state`, including on deletion retries.

## Recover an interrupted operation

```sh
mim recover dev
```

Recovery completes an interrupted creation without starting the machine. It
checks the allocated ID, or the reserved name and unique ownership token when
creation was interrupted before the ID was saved.

If allocation produced no container, delete the incomplete reservation. If
inspection reports `deleting`, retry `mim delete dev`. Failed removal or state
cleanup retains a record with the binding and error so deletion can be retried.

## Adopt an existing machine

Machines with schema-2 records require explicit adoption. Generate a plan for
one machine:

```sh
mim migrate dev --plan > dev-adoption.json
```

Review the full container ID, engine context, image, runtime, mounts, ports, and
resource settings. Apply the reviewed plan:

```sh
mim migrate dev --apply dev-adoption.json
mim inspect dev
```

Planning reads the named record and inspects that container. Apply rechecks the
original record and the exact container, saves the original bytes as
`machine.schema-2.json` beside `machine.json`, and commits the new binding.
Changed inputs require a new plan. Reapplying the committed plan is idempotent.

Adoption preserves the container, running state, storage, publication addresses,
and isolation. Since schema 2 did not store immutable IDs, review the selected
object carefully. Use one mim version as the state writer during adoption.
Keep the old executable and record backup until the handover is verified;
restoring a record cannot restore a deleted container or changed workspace.

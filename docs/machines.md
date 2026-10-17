# Machine management

Mim binds each machine to a specific container and engine context. Operations
check that binding and use the full container ID. Reusing a deleted container's
name does not replace the machine's execution target.

Podman requires local Linux. Docker also supports macOS development.

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

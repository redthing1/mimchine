#!/bin/sh
# POSIX shell + standard utilities; works with BusyBox as well as GNU tools.
set -eu
fail() { printf 'mim-limit: %s\n' "$*" >&2; exit 125; }
test "${1-}" = --memory || fail 'usage: mim-limit --memory MIB [--] COMMAND [ARG...]'
test "$#" -ge 3 || fail 'missing memory limit or command'
memory=$2
shift 2
case "$memory" in ''|*[!0-9]*) fail 'memory must be a positive integer in MiB' ;; esac
while test "${memory#0}" != "$memory"; do memory=${memory#0}; done
test -n "$memory" || fail 'memory must be positive'
if ! test "$memory" -le 8796093022207 2>/dev/null; then fail 'memory limit is too large'; fi
test "${1-}" != -- || shift
test "$#" -gt 0 || fail 'missing command'
base=/sys/fs/cgroup/jobs
test -w "$base" || fail 'job limits are not enabled; create this machine with --job-limits'
# mkdir is atomic: simultaneous wrappers cannot share a cgroup.
job="$base/job-$$"
mkdir "$job" || fail 'cannot create job cgroup'
child=
cleanup() {
    # Kill any descendants left behind by the command, including daemonized ones.
    if test -f "$job/cgroup.kill"; then printf '1\n' > "$job/cgroup.kill" || :; fi
    if test -n "$child"; then wait "$child" 2>/dev/null || :; fi
    rmdir "$job" 2>/dev/null || printf 'mim-limit: could not remove %s\n' "$job" >&2
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
printf '%s\n' "$((memory * 1048576))" > "$job/memory.max"
printf '0\n' > "$job/memory.swap.max"
printf '1\n' > "$job/memory.oom.group"
# A separate shell gives us its real PID (POSIX subshells retain the parent's $$).
# Attach before exec so allocations and all descendants are charged to the job.
sh -c 'job=$1; shift; printf "%s\n" "$$" > "$job/cgroup.procs" || exit 125; exec "$@"' \
    mim-limit-child "$job" "$@" <&0 &
child=$!
status=0
wait "$child" || status=$?
exit "$status"

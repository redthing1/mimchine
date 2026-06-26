from __future__ import annotations


# Optional image-owned service hook. mim still owns the long-lived keepalive
# shell so all runners expose the same named-machine lifecycle.
STARTUP_HOOK = "/usr/local/bin/mimchine-start"
KEEPALIVE_COMMAND = f"""
hook_pid=""
if [ -x {STARTUP_HOOK} ]; then
  {STARTUP_HOOK} &
  hook_pid="$!"
fi

stop() {{
  if [ -n "$hook_pid" ] && kill -0 "$hook_pid" 2>/dev/null; then
    kill "$hook_pid" 2>/dev/null || true
    wait "$hook_pid" 2>/dev/null || true
  fi
  exit 0
}}

trap stop TERM INT
while :; do
  sleep 60 &
  wait "$!" || true
  if [ -n "$hook_pid" ] && ! kill -0 "$hook_pid" 2>/dev/null; then
    wait "$hook_pid" 2>/dev/null || true
    hook_pid=""
  fi
done
""".strip()

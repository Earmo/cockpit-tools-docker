#!/usr/bin/env bash
set -Eeuo pipefail
export GDK_BACKEND=x11
export WEBKIT_DISABLE_DMABUF_RENDERER=1
export WEBKIT_DISABLE_COMPOSITING_MODE=1
export GTK_IM_MODULE=gtk-im-context-simple
state=$(mktemp -d /tmp/cockpit-auth.XXXXXXXX)
terminal=''
application=''
cleanup() {
    trap - EXIT INT TERM
    [[ -z $application ]] || kill "$application" 2>/dev/null || true
    [[ -z $application ]] || kill -CONT "$application" 2>/dev/null || true
    if [[ -s $state/pid ]]; then
        kill "$(cat "$state/pid")" 2>/dev/null || true
    fi
    [[ -z $terminal ]] || kill "$terminal" 2>/dev/null || true
    wait 2>/dev/null || true
    rm -rf -- "$state"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Register for the actual application PID before its updater can spawn pkexec.
# exec preserves the stopped child's PID and polkit process identity.
bash -c 'kill -STOP "$$"; exec /usr/bin/cockpit-tools' &
application=$!
stopped=0
for ((attempt=0; attempt<100; attempt++)); do
    if grep -q '^State:[[:space:]]*T' "/proc/$application/status" 2>/dev/null; then
        stopped=1
        break
    fi
    kill -0 "$application" 2>/dev/null || break
    sleep 0.01
done
((stopped)) || { printf 'Failed to prepare the application for authorization.\n' >&2; exit 1; }
xterm -T 'Cockpit Tools - Update Authorization' -geometry 90x16+40+40 \
    -fa monospace -fs 11 -e /usr/local/bin/cockpit-updater-auth "$application" "$state" &
terminal=$!
ready=0
for ((attempt=0; attempt<100; attempt++)); do
    kill -0 "$terminal" 2>/dev/null || break
    if [[ -f $state/registered && -s $state/pid ]] && kill -0 "$(cat "$state/pid")" 2>/dev/null; then
        ready=1
        break
    fi
    sleep 0.1
done
((ready)) || { printf 'Failed to register the update authentication agent.\n' >&2; exit 1; }
kill -CONT "$application"
wait "$application"

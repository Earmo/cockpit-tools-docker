#!/usr/bin/env bash
set -Eeuo pipefail
application=$1
state=$2
export LANG=C.UTF-8
printf 'Cockpit Tools update authorization\nUse the web login password when asked for the root password.\nThis window opens automatically when authorization is needed.\n\n'
xdotool windowminimize "$WINDOWID" || true
mkfifo "$state/notify"
(cat "$state/notify" >/dev/null; touch "$state/registered") &
notifier=$!
pkttyagent --process "$application" --notify-fd 3 \
    3>"$state/notify" < /dev/tty &
agent=$!
printf '%s\n' "$agent" > "$state/pid"
# The official text agent writes prompts directly to its controlling terminal.
# Its helper process marks an active authentication challenge without reading input.
(
    prompting=0
    while kill -0 "$agent" 2>/dev/null; do
        if pgrep -P "$agent" -f polkit-agent-helper-1 >/dev/null; then
            if ((prompting == 0)); then
                xdotool windowmap "$WINDOWID" windowactivate "$WINDOWID" 2>/dev/null || true
                prompting=1
            fi
        elif ((prompting == 1)); then
            xdotool windowminimize "$WINDOWID" 2>/dev/null || true
            prompting=0
        fi
        sleep 0.2
    done
) &
window_monitor=$!
trap 'kill "$agent" "$notifier" "$window_monitor" 2>/dev/null || true' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
wait "$agent"

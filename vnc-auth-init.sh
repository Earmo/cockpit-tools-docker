#!/bin/sh
set -eu
test -n "${COCKPIT_WEB_PASSWORD:-}"
test -n "${COCKPIT_WEB_USERNAME:-}"
umask 077
temporary=$(mktemp /config/.webauth-htpasswd.XXXXXX)
trap 'rm -f "$temporary"' EXIT HUP INT TERM
printf '%s\n' "$COCKPIT_WEB_PASSWORD" | htpasswd -i "$temporary" "$COCKPIT_WEB_USERNAME"
chmod 600 "$temporary"
mv -f "$temporary" /config/webauth-htpasswd

#!/bin/sh
set -eu
case "$1" in
    bus) exec /usr/bin/dbus-daemon --system --nofork --nopidfile ;;
    policy) exec /usr/lib/polkit-1/polkitd --no-debug ;;
    packages) exec /usr/libexec/packagekitd ;;
    ready)
        dbus-send --system --type=method_call --print-reply \
            --dest=org.freedesktop.DBus /org/freedesktop/DBus \
            org.freedesktop.DBus.NameHasOwner "string:$2" | grep -q 'boolean true'
        ;;
    *) exit 2 ;;
esac

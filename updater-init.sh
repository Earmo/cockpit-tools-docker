#!/bin/sh
set -eu

# Baseimage recreates passwd/group at startup, including package service users.
for account in messagebus polkitd; do
    getent group "$account" >/dev/null || groupadd --system "$account"
    getent passwd "$account" >/dev/null || useradd --system --gid "$account" \
        --home-dir /nonexistent --shell /usr/sbin/nologin "$account"
done
getent group sudo >/dev/null || groupadd --system sudo
usermod -a -G sudo root
test -n "${COCKPIT_WEB_PASSWORD:-}"
printf 'root:%s\n' "$COCKPIT_WEB_PASSWORD" | chpasswd

install -d -m 755 /run/dbus
install -d -m 700 -o polkitd -g polkitd /run/polkit-1
dbus-uuidgen --ensure=/etc/machine-id

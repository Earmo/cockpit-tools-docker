ARG BASE_IMAGE=jlesage/baseimage-gui:ubuntu-24.04-v4@sha256:e7c54da3446b4c65b6755bf00a874463c640a1f579f25bed99532051e81649ef
FROM ${BASE_IMAGE}

ENV APP_NAME="Cockpit Tools" \
    KEEP_APP_RUNNING=1 \
    LANG=zh_CN.UTF-8

# Some NAS kernels report non-executable configuration files as executable.
RUN test "$(grep -Fc 'if [ -x "$1" ]; then' /etc/cont-init.d/10-init-users.sh)" = 1 \
    && sed -i 's/if \[ -x "\$1" \]; then/if [ -f "$1" ] \&\& [ "$((0$(stat -c "%a" "$1") \& 0111))" -ne 0 ]; then/' \
       /etc/cont-init.d/10-init-users.sh \
    && sh -n /etc/cont-init.d/10-init-users.sh

ARG UBUNTU_MIRROR=http://mirrors.aliyun.com/ubuntu
RUN for source in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do \
        if [ -f "$source" ]; then \
            sed -i "s|http://archive.ubuntu.com/ubuntu|${UBUNTU_MIRROR}|g; s|http://security.ubuntu.com/ubuntu|${UBUNTU_MIRROR}|g" "$source"; \
        fi; \
    done

ARG COCKPIT_VERSION
ARG COCKPIT_SHA256
ARG COCKPIT_ARCH
COPY .runtime/package.deb /tmp/cockpit-tools.deb
# Official Linux updater: pkcon, then pkexec apt/dpkg. Runtime D-Bus and polkit
# services plus a registered authentication agent are still required.
RUN test "$(dpkg --print-architecture)" = "$COCKPIT_ARCH" \
    && printf '%s  /tmp/cockpit-tools.deb\n' "$COCKPIT_SHA256" | sha256sum -c - \
    && DEBIAN_FRONTEND=noninteractive add-pkg \
       /tmp/cockpit-tools.deb dbus-x11 gnome-keyring fonts-noto-cjk xdotool epiphany-browser \
       ca-certificates locales desktop-file-utils xdg-utils \
       dbus packagekit packagekit-tools pkexec polkitd policykit-1-gnome apt dpkg \
    && command -v pkcon \
    && command -v pkexec \
    && command -v apt \
    && command -v dpkg \
    && test -x /usr/lib/policykit-1-gnome/polkit-gnome-authentication-agent-1 \
    && dpkg-query -W cockpit-tools \
    && localedef -i zh_CN -f UTF-8 zh_CN.UTF-8 \
    && update-ca-certificates \
    && rm -f /tmp/cockpit-tools.deb \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=755 startapp-vnc.sh /startapp.sh
COPY --chmod=755 vnc-auth-init.sh /etc/cont-init.d/10-cockpit-auth.sh

RUN test "$(grep -Fc 'if [ -x "${fpath}" ]; then' /init)" = 1 \
    && test "$(grep -Fc 'if [ ! -x "${f}" ]; then' /init)" = 1 \
    && sed -i \
       -e 's/if \[ -x "\${fpath}" \]; then/if [ "$((0$(stat -c "%a" "${fpath}") \& 0111))" -ne 0 ]; then/' \
       -e 's/if \[ ! -x "\${f}" \]; then/if [ "$((0$(stat -c "%a" "${f}") \& 0111))" -eq 0 ]; then/' \
       /init \
    && sh -n /init \
    && chmod 755 /startapp.sh /etc/cont-init.d/10-cockpit-auth.sh \
    && /opt/base/sbin/su-exec 1000:1000 /bin/sh -c 'test -x /startapp.sh && test -r /startapp.sh'

LABEL org.opencontainers.image.title="Cockpit Tools VNC" \
      org.opencontainers.image.version="${COCKPIT_VERSION}" \
      org.opencontainers.image.source="https://github.com/jlcodes99/cockpit-tools"

RUN DEBIAN_FRONTEND=noninteractive add-pkg xterm \
    && command -v pkttyagent \
    && command -v chpasswd
COPY updater-init.sh /etc/cont-init.d/11-cockpit-updater.sh
COPY updater-service.sh /usr/local/bin/cockpit-updater-service
COPY updater-auth-agent.sh /usr/local/bin/cockpit-updater-auth
RUN chmod 755 /etc/cont-init.d/11-cockpit-updater.sh \
        /usr/local/bin/cockpit-updater-service /usr/local/bin/cockpit-updater-auth \
    && for spec in 'updater-bus bus org.freedesktop.DBus' \
                   'updater-policy policy org.freedesktop.PolicyKit1' \
                   'updater-packages packages org.freedesktop.PackageKit'; do \
        set -- $spec; dir="/etc/services.d/$1"; mkdir -p "$dir"; \
        printf '#!/bin/sh\nexec /usr/local/bin/cockpit-updater-service %s\n' "$2" > "$dir/run"; \
        printf '#!/bin/sh\nexec /usr/local/bin/cockpit-updater-service ready %s\n' "$3" > "$dir/is_ready"; \
        printf '#!/bin/sh\nprintf 0\n' > "$dir/uid"; \
        cp "$dir/uid" "$dir/gid"; \
        touch "$dir/respawn"; \
        chmod 755 "$dir/run" "$dir/is_ready" "$dir/uid" "$dir/gid"; \
    done \
    && touch /etc/services.d/updater-policy/updater-bus.dep \
             /etc/services.d/updater-packages/updater-policy.dep \
             /etc/services.d/app/updater-packages.dep

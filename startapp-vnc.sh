#!/bin/sh
set -eu
export GDK_BACKEND=x11
export WEBKIT_DISABLE_DMABUF_RENDERER=1
export WEBKIT_DISABLE_COMPOSITING_MODE=1
export GTK_IM_MODULE=gtk-im-context-simple
exec /usr/bin/cockpit-tools

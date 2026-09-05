#!/bin/sh
set -eu

: "${WEB_ARTIFACT_CHROMIUM_PATH:?WEB_ARTIFACT_CHROMIUM_PATH is required}"
exec /usr/bin/unshare --user --map-current-user --net -- "$WEB_ARTIFACT_CHROMIUM_PATH" "$@"

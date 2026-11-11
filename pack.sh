#!/bin/sh
# Builds the extension's release zip in dist/, ready for `gnome-extensions install`.
set -e

UUID=snip@webhookstudios.github.io
ROOT="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$ROOT/dist"
gnome-extensions pack --force --out-dir "$ROOT/dist" \
    --extra-source=picker.js \
    --extra-source=icons \
    "$ROOT/$UUID"

echo "Built $ROOT/dist/$UUID.shell-extension.zip"

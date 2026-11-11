#!/bin/sh
# Builds dist/snip_<version>_all.deb. Needs only dpkg-deb.
#
# The package installs Snip for every user of the machine:
#   /usr/share/snip/snip/                       the app (Python)
#   /usr/bin/snip                               launcher and command line
#   /usr/share/gnome-shell/extensions/<uuid>/   the shell extension (Print key shortcuts)
#   /usr/share/glib-2.0/schemas/                its settings schema
#   /usr/share/applications, /usr/share/icons   apps-menu entry and icon
set -e

APP_ID=io.github.webhookstudios.Snip
UUID=snip@webhookstudios.github.io
ROOT="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(sed -n "s/^__version__ = '\(.*\)'/\1/p" "$ROOT/snip/__init__.py")"
[ -n "$VERSION" ] || { echo "Could not read __version__ from snip/__init__.py" >&2; exit 1; }
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

SHARE="$STAGE/usr/share/snip"
EXT="$STAGE/usr/share/gnome-shell/extensions/$UUID"
ICONS="$SHARE/snip/icons/hicolor/scalable/actions"
install -d "$SHARE/snip" "$ICONS" "$EXT/icons" "$STAGE/DEBIAN" "$STAGE/usr/bin" \
    "$STAGE/usr/share/glib-2.0/schemas" \
    "$STAGE/usr/share/applications" \
    "$STAGE/usr/share/icons/hicolor/scalable/apps" \
    "$STAGE/usr/share/doc/snip"

install -m 644 "$ROOT"/snip/*.py "$SHARE/snip/"
cp -r "$ROOT/snip/sedge" "$SHARE/snip/"
find "$SHARE/snip/sedge" -type f -exec chmod 644 {} +
install -m 644 "$ROOT"/snip/icons/hicolor/scalable/actions/*.svg "$ICONS/"
install -m 755 "$ROOT/bin/snip" "$STAGE/usr/bin/snip"

install -m 644 "$ROOT/$UUID"/*.js "$ROOT/$UUID/metadata.json" \
    "$ROOT/$UUID/stylesheet.css" "$EXT/"
install -m 644 "$ROOT/$UUID"/icons/*.svg "$EXT/icons/"
# With no schemas folder of its own, the extension reads its schema from
# the system one, which dpkg compiles through glib's trigger.
install -m 644 "$ROOT/$UUID/schemas"/*.gschema.xml "$STAGE/usr/share/glib-2.0/schemas/"

install -m 644 "$ROOT/data/$APP_ID.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/"
sed -e 's|@EXEC@|snip|' -e "s|@ICON@|$APP_ID|" "$ROOT/data/$APP_ID.desktop.in" \
    > "$STAGE/usr/share/applications/$APP_ID.desktop"
chmod 644 "$STAGE/usr/share/applications/$APP_ID.desktop"

cat > "$STAGE/usr/share/doc/snip/copyright" <<EOT
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Dewdrop (formerly Snip)
Source: https://github.com/webhookstudios/snip

Files: *
Copyright: $(date +%Y) webhookstudios
License: GPL-2+
 On Debian systems, the full text of the GNU General Public License
 version 2 can be found in /usr/share/common-licenses/GPL-2.

Files: */sedge/fonts/*
Copyright: 2023 The Young Serif Project Authors; 2015 The Public Sans Project Authors
License: OFL-1.1
 The full license texts are in the sedge/fonts/ folder.
EOT
chmod 644 "$STAGE/usr/share/doc/snip/copyright"

cat > "$STAGE/DEBIAN/control" <<EOT
Package: snip
Version: $VERSION
Architecture: all
Maintainer: webhookstudios <354619278+webhookstudios@users.noreply.github.com>
Installed-Size: $(du -sk "$STAGE/usr" | cut -f1)
Depends: python3, python3-gi, python3-gi-cairo, python3-cairo, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), gir1.2-gtk-3.0, gnome-shell (>= 50~), gnome-shell (<< 51~)
Section: graphics
Priority: optional
Homepage: https://github.com/webhookstudios/snip
Description: Screenshots with markup, for GNOME
 Dewdrop (formerly Snip) takes a screenshot of an area, a window, one screen or all screens,
 from the Print key or its own window, and opens it in an editor with
 arrows, lines, boxes, ellipses, a pen, a highlighter, text, numbered
 steps, blur and pixelate, and crop. Copy the result, save it as PNG or
 JPG, or pin it on screen as an always-on-top picture.
 .
 After installing, log out and back in, then open Dewdrop from the apps menu
 and turn on its shell extension for the Print key shortcuts.
EOT

# Folders are created with the builder's umask; packages want 755.
find "$STAGE" -type d -exec chmod 755 {} +

mkdir -p "$ROOT/dist"
dpkg-deb --root-owner-group --build "$STAGE" "$ROOT/dist/snip_${VERSION}_all.deb"

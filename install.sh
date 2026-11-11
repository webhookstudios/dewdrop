#!/bin/sh
# Installs Snip for the current user, without root: the app, its apps-menu
# entry, and the GNOME Shell extension that adds the Print key shortcuts.
# Run ./install.sh --uninstall to remove it.
set -e

APP_ID=io.github.webhookstudios.Snip
UUID=snip@webhookstudios.github.io
ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
DEST="$DATA/snip"
BIN="$HOME/.local/bin/snip"
EXT="$DATA/gnome-shell/extensions/$UUID"

if [ "$1" = "--uninstall" ]; then
    gnome-extensions disable "$UUID" 2>/dev/null || true
    rm -rf "$DEST" "$BIN" "$EXT" \
        "$DATA/applications/$APP_ID.desktop" \
        "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"
    update-desktop-database "$DATA/applications" 2>/dev/null || true
    echo "Snip is removed. Your settings in $CONFIG/snip and your saved screenshots were kept."
    exit 0
fi

if ! python3 -c "import gi, cairo; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw" 2>/dev/null; then
    cat >&2 <<MSG
Snip needs Python 3 with GTK 4, libadwaita and cairo bindings, which weren't found.
Install them, then run this script again:

  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-gtk-3.0
MSG
    exit 1
fi

mkdir -p "$DEST" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps" \
    "$(dirname "$BIN")" "$DATA/gnome-shell/extensions"

# The app.
rm -rf "$DEST/snip"
cp -r "$ROOT/snip" "$DEST/snip"
find "$DEST/snip" -name __pycache__ -prune -exec rm -rf {} +
cp "$ROOT/bin/snip" "$DEST/snip-launcher"
printf '#!/bin/sh\nexec python3 "%s" "$@"\n' "$DEST/snip-launcher" > "$BIN"
chmod 755 "$BIN" "$DEST/snip-launcher"

cp "$ROOT/data/$APP_ID.svg" "$DATA/icons/hicolor/scalable/apps/"
# The icon is given by its full path: the shell does not notice a new icon
# in the user's icon folder until the next login.
ICON="$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"
sed -e "s|@EXEC@|$BIN|" -e "s|@ICON@|$ICON|" \
    "$ROOT/data/$APP_ID.desktop.in" > "$DATA/applications/$APP_ID.desktop"
update-desktop-database "$DATA/applications" 2>/dev/null || true

# The shell extension. A link here is a development setup: leave it.
if [ -L "$EXT" ]; then
    echo "Keeping existing link: $EXT"
else
    rm -rf "$EXT"
    cp -r "$ROOT/$UUID" "$EXT"
fi
glib-compile-schemas "$EXT/schemas"

# Turn it on. A brand-new extension is only found by the shell at login, so
# add it to the enabled list directly when gnome-extensions can't see it yet.
if ! gnome-extensions enable "$UUID" 2>/dev/null; then
    current="$(gsettings get org.gnome.shell enabled-extensions)"
    case "$current" in
        *"'$UUID'"*) ;;
        "@as []"|"[]") gsettings set org.gnome.shell enabled-extensions "['$UUID']" ;;
        *) gsettings set org.gnome.shell enabled-extensions "${current%]}, '$UUID']" ;;
    esac
fi

echo "Snip is installed. Open it from the apps menu, or run: snip"
echo
echo "The Print key shortcuts come from the Snip shell extension, which GNOME"
echo "only loads at login: log out and back in once to turn them on. Until then"
echo "Snip's own buttons still work, through the desktop's screenshot portal."
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "Note: $HOME/.local/bin is not on your PATH yet; log out and back in, or use $BIN." ;;
esac

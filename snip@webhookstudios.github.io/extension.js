// Snip: screenshot shortcuts and capture service for the Snip app.
//
// Plain apps on Wayland cannot read the screen or list windows without
// going through the screenshot portal. GNOME Shell can, so this extension
// does the capturing and hands the picture to the Snip editor:
//
//   * global shortcuts (Print, Alt+Print, Shift+Print, Ctrl+Print)
//   * a top-bar menu with the same captures
//   * a D-Bus service the app calls from its own buttons
//     (io.github.webhookstudios.Snip.Shell on the session bus)

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GioUnix from 'gi://GioUnix';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {Picker} from './picker.js';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot_stage_to_content');
Gio._promisify(Shell.Screenshot, 'composite_to_stream');

const APP_DESKTOP = 'io.github.webhookstudios.Snip.desktop';
const BUS_NAME = 'io.github.webhookstudios.Snip.Shell';
const OBJECT_PATH = '/io/github/webhookstudios/Snip';
const IFACE = `
<node>
  <interface name="${BUS_NAME}">
    <method name="Capture">
      <arg type="s" name="mode" direction="in"/>
      <arg type="s" name="path" direction="out"/>
      <arg type="i" name="x" direction="out"/>
      <arg type="i" name="y" direction="out"/>
    </method>
    <method name="SetClipboardImage">
      <arg type="s" name="path" direction="in"/>
    </method>
    <property name="Version" type="s" access="read"/>
  </interface>
</node>`;

// Our shortcut keys, and the GNOME keys that would grab the same accelerator.
const SHORTCUTS = {
    'shortcut-region': 'area',
    'shortcut-window': 'window',
    'shortcut-screen': 'screen',
    'shortcut-all-screens': 'all',
};
const SHELL_KEYS = ['show-screenshot-ui', 'screenshot', 'screenshot-window'];

function normalizeAccel(accel) {
    return accel.replace(/<Primary>|<Ctrl>/g, '<Control>').toLowerCase();
}

export default class SnipExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._busy = false;
        this._picker = null;

        this._takeOverShellKeys();
        for (const [key, mode] of Object.entries(SHORTCUTS)) {
            Main.wm.addKeybinding(key, this._settings, Meta.KeyBindingFlags.NONE,
                Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW | Shell.ActionMode.POPUP,
                () => this._fromShortcut(mode));
        }
        this._shortcutsChangedId = this._settings.connect('changed', (_s, key) => {
            if (key in SHORTCUTS || key === 'take-over-shell-keys') {
                this._restoreShellKeys();
                this._takeOverShellKeys();
            }
            if (key === 'show-indicator')
                this._syncIndicator();
        });

        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
        this._ownerId = Gio.bus_own_name_on_connection(Gio.DBus.session, BUS_NAME,
            Gio.BusNameOwnerFlags.REPLACE, null, null);

        this._syncIndicator();
    }

    disable() {
        this._picker?.cancel();
        this._picker = null;
        this._indicator?.destroy();
        this._indicator = null;
        for (const key of Object.keys(SHORTCUTS))
            Main.wm.removeKeybinding(key);
        if (this._shortcutsChangedId)
            this._settings.disconnect(this._shortcutsChangedId);
        this._shortcutsChangedId = 0;
        this._restoreShellKeys();
        if (this._ownerId)
            Gio.bus_unown_name(this._ownerId);
        this._ownerId = 0;
        this._dbus?.unexport();
        this._dbus = null;
        if (this._delayId)
            GLib.source_remove(this._delayId);
        this._delayId = 0;
        this._settings = null;
    }

    // ---- GNOME's own screenshot keys ---------------------------------

    _takeOverShellKeys() {
        if (!this._settings.get_boolean('take-over-shell-keys'))
            return;
        const ours = new Set();
        for (const key of Object.keys(SHORTCUTS)) {
            for (const a of this._settings.get_strv(key))
                ours.add(normalizeAccel(a));
        }
        const shell = new Gio.Settings({schema_id: 'org.gnome.shell.keybindings'});
        let saved = {};
        try {
            saved = JSON.parse(this._settings.get_string('saved-shell-keys') || '{}');
        } catch {
            saved = {};
        }
        for (const key of SHELL_KEYS) {
            const current = shell.get_strv(key);
            const keep = current.filter(a => !ours.has(normalizeAccel(a)));
            if (keep.length === current.length)
                continue;
            // Left over from a crash: keep the first saved value, it is GNOME's.
            if (!(key in saved))
                saved[key] = current;
            shell.set_strv(key, keep);
        }
        this._settings.set_string('saved-shell-keys', JSON.stringify(saved));
    }

    _restoreShellKeys() {
        let saved = {};
        try {
            saved = JSON.parse(this._settings.get_string('saved-shell-keys') || '{}');
        } catch {
            saved = {};
        }
        const shell = new Gio.Settings({schema_id: 'org.gnome.shell.keybindings'});
        for (const [key, value] of Object.entries(saved)) {
            if (!SHELL_KEYS.includes(key) || !Array.isArray(value))
                continue;
            const current = shell.get_strv(key);
            shell.set_strv(key, [...new Set([...current, ...value])]);
        }
        this._settings.set_string('saved-shell-keys', '');
    }

    // ---- Top-bar menu ---------------------------------------------------

    _syncIndicator() {
        const show = this._settings.get_boolean('show-indicator');
        if (!show) {
            this._indicator?.destroy();
            this._indicator = null;
            return;
        }
        if (this._indicator)
            return;

        const indicator = new PanelMenu.Button(0.5, 'Snip');
        indicator.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(`${this.path}/icons/snip-symbolic.svg`),
            style_class: 'system-status-icon',
        }));
        const menu = indicator.menu;
        const add = (label, accelKey, mode) => {
            const item = new PopupMenu.PopupMenuItem(label);
            const accel = accelKey ? this._settings.get_strv(accelKey)[0] : null;
            if (accel) {
                item.add_child(new St.Label({
                    text: accel.replace(/<Control>|<Primary>/g, 'Ctrl+')
                        .replace(/<Alt>/g, 'Alt+').replace(/<Shift>/g, 'Shift+')
                        .replace(/<Super>/g, 'Super+'),
                    x_expand: true,
                    x_align: Clutter.ActorAlign.END,
                    style_class: 'popup-inactive-menu-item',
                }));
            }
            item.connect('activate', () => this._afterMenu(() => this._fromShortcut(mode)));
            menu.addMenuItem(item);
        };
        add('Capture Area or Window', 'shortcut-region', 'area');
        add('Capture Window', 'shortcut-window', 'window');
        add('Capture Screen', 'shortcut-screen', 'screen');
        add('Capture All Screens', 'shortcut-all-screens', 'all');
        menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        const open = new PopupMenu.PopupMenuItem('Open Snip');
        open.connect('activate', () => this._launch([]));
        menu.addMenuItem(open);
        const last = new PopupMenu.PopupMenuItem('Open Last Capture');
        last.connect('activate', () => this._launch(['--last']));
        menu.addMenuItem(last);

        this._indicator = indicator;
        Main.panel.addToStatusArea(this.uuid, indicator);
    }

    _afterMenu(fn) {
        // Let the menu close (and fade out) before the screen is frozen.
        if (this._delayId)
            GLib.source_remove(this._delayId);
        this._delayId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 250, () => {
            this._delayId = 0;
            fn();
            return GLib.SOURCE_REMOVE;
        });
    }

    // ---- Capturing ------------------------------------------------------

    async _fromShortcut(mode) {
        const result = await this._capture(mode);
        if (result)
            this._launch(['--edit', result.path, '--origin', `${result.x},${result.y}`,
                '--mode', mode]);
    }

    /**
     * @param {string} mode - 'area', 'window', 'screen' or 'all'
     * @returns {Promise<null|{path: string, x: number, y: number}>}
     */
    async _capture(mode) {
        if (this._busy)
            return null;
        this._busy = true;
        try {
            return await this._doCapture(mode);
        } catch (e) {
            logError(e, 'Snip: capture failed');
            Main.notifyError('Snip', `The screenshot failed: ${e.message}`);
            return null;
        } finally {
            this._busy = false;
        }
    }

    async _doCapture(mode) {
        const shooter = new Shell.Screenshot();
        const [content, scale] = await shooter.screenshot_stage_to_content();
        const texture = content.get_texture();
        let geometry = null; // logical [x, y, w, h], null = everything
        let windowActor = null;

        if (mode === 'area' || mode === 'window') {
            this._picker = new Picker(mode, content);
            const choice = await this._picker.open();
            this._picker = null;
            if (!choice)
                return null;
            if (mode === 'window' && choice.window)
                windowActor = choice.window;
            else
                geometry = choice.rect;
            if (mode === 'window' && !windowActor)
                return null;
        } else if (mode === 'screen') {
            const [px, py] = global.get_pointer();
            const mon = Main.layoutManager.monitors.find(m =>
                px >= m.x && py >= m.y && px < m.x + m.width && py < m.y + m.height) ??
                Main.layoutManager.primaryMonitor;
            geometry = [mon.x, mon.y, mon.width, mon.height];
        } else if (mode !== 'all') {
            throw new Error(`Unknown capture mode "${mode}"`);
        }

        const stream = Gio.MemoryOutputStream.new_resizable();
        let origin = [0, 0];
        if (windowActor) {
            // The window's own picture, even the parts other windows cover,
            // cut down to its frame so client-side shadows are left out.
            const win = windowActor.meta_window;
            const frame = win.get_frame_rect();
            const buffer = win.get_buffer_rect();
            const s = windowActor.get_resource_scale();
            const winContent = windowActor.paint_to_content(null);
            await Shell.Screenshot.composite_to_stream(winContent.get_texture(),
                Math.round((frame.x - buffer.x) * s), Math.round((frame.y - buffer.y) * s),
                Math.round(frame.width * s), Math.round(frame.height * s),
                s, null, 0, 0, 1, stream);
            origin = [frame.x, frame.y];
        } else {
            const [x, y, w, h] = geometry ?? [0, 0, -1, -1];
            const g = geometry
                ? [x, y, w, h].map(v => Math.round(v * scale))
                : [0, 0, -1, -1];
            await Shell.Screenshot.composite_to_stream(texture, ...g, scale,
                null, 0, 0, 1, stream);
            if (geometry)
                origin = [x, y];
        }
        stream.close(null);

        global.display.get_sound_player().play_from_theme(
            'screen-capture', 'Screenshot taken', null);

        const dir = GLib.build_filenamev([GLib.get_user_cache_dir(), 'snip', 'captures']);
        GLib.mkdir_with_parents(dir, 0o700);
        const stamp = GLib.DateTime.new_now_local().format('%Y%m%d-%H%M%S');
        const name = `capture-${stamp}-${GLib.random_int_range(1000, 9999)}.png`;
        const path = GLib.build_filenamev([dir, name]);
        Gio.File.new_for_path(path).replace_contents(stream.steal_as_bytes().toArray(),
            null, false, Gio.FileCreateFlags.PRIVATE, null);
        return {path, x: Math.round(origin[0]), y: Math.round(origin[1])};
    }

    // ---- Launching the app -----------------------------------------------

    _appArgv() {
        const info = GioUnix.DesktopAppInfo.new(APP_DESKTOP);
        if (info) {
            try {
                const [, argv] = GLib.shell_parse_argv(info.get_string('Exec'));
                const cmd = argv.filter(a => !/^%[a-zA-Z]$/.test(a));
                if (cmd.length)
                    return cmd;
            } catch (e) {
                logError(e, 'Snip: could not read the app launcher');
            }
        }
        const found = GLib.find_program_in_path('snip');
        return [found ?? GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'snip'])];
    }

    _launch(args) {
        try {
            const launcher = new Gio.SubprocessLauncher({flags: Gio.SubprocessFlags.NONE});
            launcher.spawnv([...this._appArgv(), ...args]);
        } catch (e) {
            logError(e, 'Snip: could not start the app');
            Main.notifyError('Snip', 'Could not open the Snip app. Is it installed?');
        }
    }

    // ---- D-Bus methods ----------------------------------------------------

    async CaptureAsync([mode], invocation) {
        const result = await this._capture(mode);
        invocation.return_value(new GLib.Variant('(sii)',
            result ? [result.path, result.x, result.y] : ['', 0, 0]));
    }

    SetClipboardImage(path) {
        const [, bytes] = GLib.file_get_contents(path);
        St.Clipboard.get_default().set_content(St.ClipboardType.CLIPBOARD,
            'image/png', new GLib.Bytes(bytes));
    }

    get Version() {
        return this.metadata['version-name'] ?? '';
    }
}

"""The start window (capture buttons) and the preferences dialog."""
import os

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import capture  # noqa: E402
from .config import PATTERN_HELP, expand_pattern  # noqa: E402

SCHEMA_ID = 'org.gnome.shell.extensions.dewdrop'
CAPTURES = [
    ('area', 'Area', 'dewdrop-region-symbolic', 'shortcut-region',
     'Drag out an area, or click a window'),
    ('window', 'Window', 'dewdrop-window-symbolic', 'shortcut-window', 'Click the window to take'),
    ('screen', 'Screen', 'dewdrop-screen-symbolic', 'shortcut-screen',
     'The screen the pointer is on'),
    ('all', 'All Screens', 'dewdrop-screens-symbolic', 'shortcut-all-screens',
     'Every monitor in one picture'),
]


def extension_settings():
    """The extension's settings, or None when its schema isn't installed."""
    source = Gio.SettingsSchemaSource.get_default()
    schema = source.lookup(SCHEMA_ID, True) if source else None
    if schema is None:
        for base in (os.path.join(GLib.get_user_data_dir(), 'gnome-shell', 'extensions'),
                     '/usr/share/gnome-shell/extensions'):
            d = os.path.join(base, capture.EXT_UUID, 'schemas')
            if os.path.exists(os.path.join(d, 'gschemas.compiled')):
                src = Gio.SettingsSchemaSource.new_from_directory(d, source, False)
                schema = src.lookup(SCHEMA_ID, False)
                if schema:
                    break
    return Gio.Settings.new_full(schema, None, None) if schema else None


def accel_label(accel):
    ok, key, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_get_label(key, mods) if ok and key else accel


class LauncherWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='Dewdrop', default_width=520,
                         resizable=False)
        self.app = app
        self.settings = extension_settings()

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append('Open image…', 'win.open')
        menu.append('Open last capture', 'win.last')
        section = Gio.Menu()
        section.append('Settings', 'app.preferences')
        section.append('About Dewdrop', 'app.about')
        menu.append_section(None, section)
        header.pack_end(Gtk.MenuButton(icon_name='open-menu-symbolic', menu_model=menu,
                                       primary=True, tooltip_text='Menu'))
        view.add_top_bar(header)
        for name, cb in (('open', self.open_image), ('last', self.open_last)):
            act = Gio.SimpleAction.new(name, None)
            act.connect('activate', lambda *_a, cb=cb: cb())
            self.add_action(act)

        self.banner = Adw.Banner()
        self.banner.connect('button-clicked', self.on_banner)
        view.add_top_bar(self.banner)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18,
                      margin_bottom=24, margin_start=24, margin_end=24)
        grid = Gtk.Grid(column_spacing=12, row_spacing=12, column_homogeneous=True)
        self.shortcut_labels = {}
        for i, (mode, label, icon, key, hint) in enumerate(CAPTURES):
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                            margin_top=14, margin_bottom=12)
            inner.append(Gtk.Image(icon_name=icon, pixel_size=32))
            inner.append(Gtk.Label(label=label, css_classes=['heading']))
            sc = Gtk.Label(css_classes=['caption', 'dim-label'])
            inner.append(sc)
            self.shortcut_labels[key] = sc
            b = Gtk.Button(child=inner, tooltip_text=hint, css_classes=['card'])
            b.connect('clicked', lambda *_a, m=mode: self.start_capture(m))
            grid.attach(b, i % 2, i // 2, 1, 1)
        box.append(grid)

        group = Adw.PreferencesGroup()
        self.delay = Adw.SpinRow.new_with_range(0, 30, 1)
        self.delay.set_title('Delay')
        self.delay.set_subtitle('Seconds to wait before capturing')
        self.delay.set_value(app.cfg['delay'])
        self.delay.connect('notify::value',
                           lambda r, _p: app.cfg.__setitem__('delay', int(r.get_value())))
        group.add(self.delay)
        box.append(group)

        view.set_content(box)
        self.set_content(view)
        self.refresh()
        if self.settings:
            self.settings.connect('changed', lambda *_: self.refresh())
        # The extension may come and go (enabled, disabled, logged in).
        self._poll = GLib.timeout_add_seconds(3, self._refresh_timer)
        self.connect('destroy', lambda *_: GLib.source_remove(self._poll))

    def _refresh_timer(self):
        self.refresh()
        return True

    def refresh(self):
        state = capture.extension_state()
        self.state = state
        text = {
            'missing': 'Install the Dewdrop shell extension for the Print key and silent captures',
            'needs-logout': 'Log out and back in to turn on the Print key shortcuts',
            'off': 'The Dewdrop shell extension is off, so the Print key does not open Dewdrop',
            'error': 'The Dewdrop shell extension failed to start',
        }.get(state)
        self.banner.set_title(text or '')
        self.banner.set_button_label('Turn On' if state == 'off' else None)
        self.banner.set_revealed(bool(text))
        for _m, _l, _i, key, _h in CAPTURES:
            label = self.shortcut_labels[key]
            accels = self.settings.get_strv(key) if self.settings and state == 'active' else []
            label.set_label(accel_label(accels[0]) if accels else ' ')

    def on_banner(self, *_):
        if self.state == 'off':
            capture.enable_extension()
            GLib.timeout_add(800, lambda: (self.refresh(), False)[1])

    def start_capture(self, mode):
        monitor = None
        surface = self.get_surface()
        if surface is not None:
            monitor = Gdk.Display.get_default().get_monitor_at_surface(surface)
        self.set_visible(False)

        def cancelled():
            self.set_visible(True)
            self.present()

        def after():
            self.app.capture(mode, int(self.delay.get_value()), monitor=monitor,
                             on_cancel=cancelled)
            # Close once the capture is handed over; it comes back if cancelled.
            return False
        # Give the window time to disappear before the screen is read.
        GLib.timeout_add(350, after)

    def open_image(self):
        dialog = Gtk.FileDialog(title='Open Image')
        f = Gtk.FileFilter(name='Images')
        f.add_pixbuf_formats()
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dialog.set_filters(filters)

        def done(d, res):
            try:
                file = d.open_finish(res)
            except GLib.Error:
                return
            self.app.open_editor(file.get_path())
        dialog.open(self, None, done)

    def open_last(self):
        last = self.app.cfg['last_capture']
        if last and os.path.exists(last):
            self.app.open_editor(last)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, app):
        super().__init__(title='Settings')
        self.app = app
        cfg = app.cfg
        page = Adw.PreferencesPage(title='General', icon_name='emblem-system-symbolic')
        self.add(page)

        after = Adw.PreferencesGroup(
            title='After a shortcut capture',
            description='What each keyboard shortcut (and the top-bar menu) does with the '
                        'picture. Capturing from Dewdrop\'s own window always opens the editor.')
        actions = [('editor', 'Open the editor'), ('save', 'Save straight away'),
                   ('copy', 'Copy to the clipboard'), ('pin', 'Pin on screen')]
        for mode, label, _i, _key, _h in CAPTURES:
            row = Adw.ComboRow(title=label, model=Gtk.StringList.new([x[1] for x in actions]))
            row.set_selected(next((i for i, x in enumerate(actions)
                                   if x[0] == cfg['shortcut_actions'].get(mode)), 0))

            def changed(r, _p, mode=mode):
                cfg['shortcut_actions'] = {**cfg['shortcut_actions'],
                                           mode: actions[r.get_selected()][0]}
                self.sync_quick_folder()
            row.connect('notify::selected', changed)
            after.add(row)
        self.quick_row = Adw.ActionRow(title='Save straight away to',
                                       subtitle=_tilde(cfg['quick_save_dir']), activatable=True)
        self.quick_row.add_suffix(Gtk.Image(icon_name='folder-open-symbolic'))
        self.quick_row.connect('activated', lambda *_: self.choose_folder(key='quick_save_dir',
                                                                          row=self.quick_row))
        after.add(self.quick_row)
        page.add(after)
        self.sync_quick_folder()

        save = Adw.PreferencesGroup(title='Saving')
        self.folder_row = Adw.ActionRow(title='Editor saves to', subtitle=_tilde(cfg['save_dir']),
                                        activatable=True)
        self.folder_row.add_suffix(Gtk.Image(icon_name='folder-open-symbolic'))
        self.folder_row.connect('activated', lambda *_: self.choose_folder())
        save.add(self.folder_row)

        self.pattern = Adw.EntryRow(title='File name', text=cfg['pattern'],
                                    show_apply_button=True)
        self.pattern.connect('apply', self.on_pattern)
        self.pattern.connect('changed', self.on_pattern_preview)
        save.add(self.pattern)
        self.preview = Adw.ActionRow(title='Example', subtitle_lines=3)
        self.preview.add_css_class('property')
        save.add(self.preview)
        self.on_pattern_preview(self.pattern)

        fmt = Adw.ComboRow(title='Format', model=Gtk.StringList.new(['PNG', 'JPG']))
        fmt.set_selected(1 if cfg['format'] == 'jpg' else 0)
        quality = Adw.SpinRow.new_with_range(10, 100, 5)
        quality.set_title('JPG quality')
        quality.set_value(cfg['jpeg_quality'])
        quality.set_sensitive(cfg['format'] == 'jpg')

        def on_fmt(r, _p):
            cfg['format'] = 'jpg' if r.get_selected() == 1 else 'png'
            quality.set_sensitive(cfg['format'] == 'jpg')
            self.on_pattern_preview(self.pattern)
        fmt.connect('notify::selected', on_fmt)
        quality.connect('notify::value',
                        lambda r, _p: cfg.__setitem__('jpeg_quality', int(r.get_value())))
        save.add(fmt)
        save.add(quality)
        copy_too = Adw.SwitchRow(title='Also copy when saving', active=cfg['copy_after_save'])
        copy_too.connect('notify::active',
                         lambda r, _p: cfg.__setitem__('copy_after_save', r.get_active()))
        save.add(copy_too)
        page.add(save)

        keys = Adw.PreferencesGroup(title='Shortcuts')
        settings = extension_settings()
        if settings is None:
            keys.set_description('Shortcuts need the Dewdrop shell extension. '
                                 'Install Dewdrop with its install script or deb package, '
                                 'then log out and back in.')
        else:
            keys.set_description('Type a shortcut such as Print, &lt;Alt&gt;Print or '
                                 '&lt;Ctrl&gt;&lt;Shift&gt;s, then press Enter. Leave it empty to turn '
                                 'a shortcut off.')
            for _m, label, _i, key, _h in CAPTURES:
                keys.add(self.shortcut_row(settings, key, label))
            take = Adw.SwitchRow(title="Use GNOME's screenshot keys",
                                 subtitle="Frees Print, Shift+Print and Alt+Print from GNOME's "
                                          "own screenshot tool while Dewdrop is on")
            settings.bind('take-over-shell-keys', take, 'active', Gio.SettingsBindFlags.DEFAULT)
            keys.add(take)
            ind = Adw.SwitchRow(title='Show Dewdrop in the top bar')
            settings.bind('show-indicator', ind, 'active', Gio.SettingsBindFlags.DEFAULT)
            keys.add(ind)
        page.add(keys)

    def shortcut_row(self, settings, key, label):
        row = Adw.EntryRow(title=label, show_apply_button=True,
                           text=(settings.get_strv(key) or [''])[0])

        def apply(r):
            text = r.get_text().strip()
            if not text:
                settings.set_strv(key, [])
                return
            ok, k, mods = Gtk.accelerator_parse(text)
            if not ok or not k:
                r.add_css_class('error')
                return
            r.remove_css_class('error')
            settings.set_strv(key, [Gtk.accelerator_name(k, mods)])
            r.set_text(Gtk.accelerator_name(k, mods))
        row.connect('apply', apply)
        return row

    def sync_quick_folder(self):
        self.quick_row.set_sensitive('save' in self.app.cfg['shortcut_actions'].values())

    def choose_folder(self, key='save_dir', row=None):
        row = row or self.folder_row
        dialog = Gtk.FileDialog(title='Screenshots Folder')
        folder = os.path.expanduser(self.app.cfg[key])
        if os.path.isdir(folder):
            dialog.set_initial_folder(Gio.File.new_for_path(folder))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            self.app.cfg[key] = f.get_path()
            row.set_subtitle(_tilde(f.get_path()))
        dialog.select_folder(self.get_root(), None, done)

    def on_pattern(self, row):
        text = row.get_text().strip() or 'Snip {YYYY}-{MM}-{DD} {hh}-{mm}-{ss}'
        self.app.cfg['pattern'] = text

    def on_pattern_preview(self, row):
        ext = 'jpg' if self.app.cfg['format'] == 'jpg' else 'png'
        self.preview.set_subtitle(f'{expand_pattern(row.get_text() or "Snip")}.{ext}\n'
                                  f'{PATTERN_HELP}')


def _tilde(path):
    home = os.path.expanduser('~')
    return '~' + path[len(home):] if path == home or path.startswith(home + os.sep) else path

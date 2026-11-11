"""Taking the screenshot.

With the Snip shell extension on, the shell does the capture: silent, with
a frozen-screen area and window picker. Without it, Snip falls back to the
desktop's screenshot portal (org.freedesktop.portal.Screenshot), which
every Wayland desktop offers:

  all screens   portal, silent
  one screen    portal, silent, cut to that monitor
  area          portal, silent, then Snip's own full-screen area picker
  window        portal, interactive: GNOME's screenshot window where you
                pick the window yourself
"""
import os
import shutil
from urllib.parse import unquote, urlparse

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

import cairo  # noqa: E402

from .config import new_cache_path  # noqa: E402

SHELL_NAME = 'io.github.webhookstudios.Snip.Shell'
SHELL_PATH = '/io/github/webhookstudios/Snip'
EXT_UUID = 'snip@webhookstudios.github.io'
MODES = ('area', 'window', 'screen', 'all')


def _bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def shell_available():
    """True when the Snip extension is running in GNOME Shell."""
    try:
        res = _bus().call_sync('org.freedesktop.DBus', '/org/freedesktop/DBus',
                               'org.freedesktop.DBus', 'NameHasOwner',
                               GLib.Variant('(s)', (SHELL_NAME,)), GLib.VariantType('(b)'),
                               Gio.DBusCallFlags.NONE, 1000, None)
        return res.unpack()[0]
    except GLib.Error:
        return False


def extension_state():
    """'active', 'off', 'needs-logout', 'error' or 'missing'."""
    if shell_available():
        return 'active'
    try:
        res = _bus().call_sync('org.gnome.Shell.Extensions', '/org/gnome/Shell/Extensions',
                               'org.gnome.Shell.Extensions', 'GetExtensionInfo',
                               GLib.Variant('(s)', (EXT_UUID,)), None,
                               Gio.DBusCallFlags.NONE, 2000, None)
        info = res.unpack()[0]
    except GLib.Error:
        info = {}
    if not info:
        # The shell only finds new extensions at login.
        for base in (os.path.join(GLib.get_user_data_dir(), 'gnome-shell', 'extensions'),
                     '/usr/share/gnome-shell/extensions'):
            if os.path.isdir(os.path.join(base, EXT_UUID)):
                return 'needs-logout'
        return 'missing'
    state = info.get('state', 0)
    if state == 1:   # ENABLED, but our name is not on the bus yet
        return 'active'
    if state in (3, 4):  # ERROR, OUT_OF_DATE
        return 'error'
    return 'off'


def enable_extension():
    try:
        _bus().call_sync('org.gnome.Shell.Extensions', '/org/gnome/Shell/Extensions',
                         'org.gnome.Shell.Extensions', 'EnableExtension',
                         GLib.Variant('(s)', (EXT_UUID,)), None,
                         Gio.DBusCallFlags.NONE, 5000, None)
        return True
    except GLib.Error:
        return False


def shell_set_clipboard(path):
    try:
        _bus().call_sync(SHELL_NAME, SHELL_PATH, SHELL_NAME, 'SetClipboardImage',
                         GLib.Variant('(s)', (path,)), None, Gio.DBusCallFlags.NONE, 5000, None)
        return True
    except GLib.Error:
        return False


class Capture:
    """Runs one capture and calls done(path, origin) or done(None, None)."""

    def __init__(self, app, mode, done, monitor=None):
        assert mode in MODES
        self.app = app
        self.mode = mode
        self.done = done
        self.monitor = monitor   # Gdk.Monitor for 'screen' in fallback mode

    def start(self):
        if shell_available():
            self._via_shell()
        else:
            self._via_portal(interactive=self.mode == 'window')

    def _finish(self, path, origin=None):
        GLib.idle_add(lambda: (self.done(path, origin), False)[1])

    # ---- extension ---------------------------------------------------

    def _via_shell(self):
        def reply(bus, res):
            try:
                path, x, y = bus.call_finish(res).unpack()
            except GLib.Error as e:
                print(f'snip: shell capture failed: {e.message}')
                path, x, y = '', 0, 0
            self._finish(path or None, (x, y) if path else None)
        _bus().call(SHELL_NAME, SHELL_PATH, SHELL_NAME, 'Capture',
                    GLib.Variant('(s)', (self.mode,)), GLib.VariantType('(sii)'),
                    Gio.DBusCallFlags.NONE, GLib.MAXINT32, None, reply)

    # ---- portal --------------------------------------------------------

    def _via_portal(self, interactive):
        bus = _bus()
        token = f'snip{GLib.random_int_range(0, 1 << 30)}'
        sender = bus.get_unique_name()[1:].replace('.', '_')
        handle = f'/org/freedesktop/portal/desktop/request/{sender}/{token}'
        sub = [0]

        def response(_c, _s, _p, _i, _sig, params):
            bus.signal_unsubscribe(sub[0])
            code, results = params.unpack()
            uri = results.get('uri') if code == 0 else None
            if not uri:
                self._finish(None)
                return
            src = unquote(urlparse(uri).path)
            # The portal saves into ~/Pictures; move it out of the way.
            path = new_cache_path()
            try:
                shutil.move(src, path)
            except OSError:
                path = src
            self._after_portal(path)

        sub[0] = bus.signal_subscribe('org.freedesktop.portal.Desktop',
                                      'org.freedesktop.portal.Request', 'Response',
                                      handle, None, Gio.DBusSignalFlags.NO_MATCH_RULE, response)
        options = {'handle_token': GLib.Variant('s', token),
                   'interactive': GLib.Variant('b', interactive),
                   'modal': GLib.Variant('b', False)}

        def called(b, res):
            try:
                b.call_finish(res)
            except GLib.Error as e:
                bus.signal_unsubscribe(sub[0])
                print(f'snip: screenshot portal failed: {e.message}')
                self._finish(None)
        bus.call('org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop',
                 'org.freedesktop.portal.Screenshot', 'Screenshot',
                 GLib.Variant('(sa{sv})', ('', options)), None,
                 Gio.DBusCallFlags.NONE, GLib.MAXINT32, None, called)

    def _after_portal(self, path):
        if self.mode in ('all', 'window'):
            self._finish(path, None if self.mode == 'window' else (0, 0))
            return
        surface = cairo.ImageSurface.create_from_png(path)
        layout = monitor_layout(surface)
        if self.mode == 'screen':
            mon = self.monitor or (layout[0][0] if layout else None)
            entry = next((e for e in layout if e[0] == mon), layout[0] if layout else None)
            if not entry:
                self._finish(path, (0, 0))
                return
            _m, logical, px = entry
            out = crop_surface(surface, *px)
            out.write_to_png(path)
            self._finish(path, (logical.x, logical.y))
            return
        AreaPicker(self.app, surface, layout,
                   lambda rect: self._area_chosen(surface, path, rect)).show()

    def _area_chosen(self, surface, path, rect):
        if not rect:
            self._finish(None)
            return
        px, logical_origin = rect
        crop_surface(surface, *px).write_to_png(path)
        self._finish(path, logical_origin)


def crop_surface(surface, x, y, w, h):
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, int(w)), max(1, int(h)))
    cr = cairo.Context(out)
    cr.set_source_surface(surface, -x, -y)
    cr.paint()
    return out


def monitor_layout(surface):
    """[(monitor, logical geometry, (x, y, w, h) in picture pixels), ...]

    The portal returns one picture of the whole desktop. Monitors are given
    in logical coordinates; the picture can be bigger when a monitor is scaled.
    """
    monitors = Gdk.Display.get_default().get_monitors()
    geos = [(monitors.get_item(i), monitors.get_item(i).get_geometry())
            for i in range(monitors.get_n_items())]
    if not geos:
        return []
    minx = min(g.x for _m, g in geos)
    miny = min(g.y for _m, g in geos)
    maxx = max(g.x + g.width for _m, g in geos)
    maxy = max(g.y + g.height for _m, g in geos)
    sx = surface.get_width() / max(1, maxx - minx)
    sy = surface.get_height() / max(1, maxy - miny)
    return [(m, g, (round((g.x - minx) * sx), round((g.y - miny) * sy),
                    round(g.width * sx), round(g.height * sy))) for m, g in geos]


class AreaPicker:
    """Fallback area picker: the frozen screen, full-screen on every monitor."""

    def __init__(self, app, surface, layout, done):
        self.app = app
        self.surface = surface
        self.done = done
        self.windows = []
        self.finished = False
        for monitor, logical, px in layout:
            self.windows.append(_PickerWindow(self, monitor, logical, px))

    def show(self):
        for w in self.windows:
            w.present()
            w.fullscreen_on_monitor(w.monitor)

    def finish(self, result):
        if self.finished:
            return
        self.finished = True
        for w in self.windows:
            w.destroy()
        self.done(result)


class _PickerWindow(Gtk.Window):
    def __init__(self, picker, monitor, logical, px):
        super().__init__(application=picker.app, decorated=False, title='Dewdrop area picker')
        self.picker = picker
        self.monitor = monitor
        self.logical = logical
        self.px = px
        self.start = None
        self.rect = None   # logical, relative to this monitor
        area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        area.set_draw_func(self.draw)
        area.set_cursor(Gdk.Cursor.new_from_name('crosshair'))
        self.area = area
        self.set_child(area)
        drag = Gtk.GestureDrag()
        drag.connect('drag-begin', self.on_begin)
        drag.connect('drag-update', self.on_update)
        drag.connect('drag-end', self.on_end)
        area.add_controller(drag)
        click = Gtk.GestureClick(button=3)
        click.connect('pressed', lambda *_: self.picker.finish(None))
        area.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.on_key)
        self.add_controller(keys)

    def draw(self, _a, cr, w, h):
        x, y, pw, ph = self.px
        cr.save()
        cr.scale(w / max(1, pw), h / max(1, ph))
        cr.set_source_surface(self.picker.surface, -x, -y)
        cr.paint()
        cr.restore()
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.rectangle(0, 0, w, h)
        if self.rect:
            rx, ry, rw, rh = self.rect
            cr.rectangle(rx + rw, ry, -rw, rh)   # cut a hole (opposite winding)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.fill()
        if self.rect:
            rx, ry, rw, rh = self.rect
            cr.set_source_rgba(0.21, 0.52, 0.89, 1)
            cr.set_line_width(2)
            cr.rectangle(rx - 1, ry - 1, rw + 2, rh + 2)
            cr.stroke()
            label = f'{round(rw * pw / w)} × {round(rh * ph / h)}'
            cr.select_font_face('Sans')
            cr.set_font_size(13)
            ext = cr.text_extents(label)
            ly = ry - 10 if ry > 30 else ry + ext.height + 12
            cr.set_source_rgba(0.12, 0.12, 0.12, 0.85)
            cr.rectangle(rx, ly - ext.height - 6, ext.width + 14, ext.height + 10)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 1)
            cr.move_to(rx + 7, ly)
            cr.show_text(label)
        else:
            text = 'Drag to select an area  ·  Esc to cancel'
            cr.select_font_face('Sans')
            cr.set_font_size(15)
            ext = cr.text_extents(text)
            cx, cy = (w - ext.width) / 2, h - 60
            cr.set_source_rgba(0.12, 0.12, 0.12, 0.85)
            cr.rectangle(cx - 16, cy - ext.height - 10, ext.width + 32, ext.height + 20)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 1)
            cr.move_to(cx, cy)
            cr.show_text(text)

    def on_begin(self, _g, x, y):
        self.start = (x, y)
        self.rect = None
        for w in self.picker.windows:
            if w is not self:
                w.rect = None
                w.area.queue_draw()

    def on_update(self, _g, dx, dy):
        sx, sy = self.start
        self.rect = (min(sx, sx + dx), min(sy, sy + dy), abs(dx), abs(dy))
        self.area.queue_draw()

    def on_end(self, _g, dx, dy):
        self.on_update(_g, dx, dy)
        rx, ry, rw, rh = self.rect
        if rw < 3 or rh < 3:
            # A click: take the whole monitor.
            rx, ry, rw, rh = 0, 0, self.area.get_width(), self.area.get_height()
        x, y, pw, ph = self.px
        w, h = self.area.get_width(), self.area.get_height()
        sx, sy = pw / max(1, w), ph / max(1, h)
        result = ((x + rx * sx, y + ry * sy, rw * sx, rh * sy),
                  (round(self.logical.x + rx * self.logical.width / max(1, w)),
                   round(self.logical.y + ry * self.logical.height / max(1, h))))
        self.picker.finish(result)

    def on_key(self, _c, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self.picker.finish(None)
            return True
        return False

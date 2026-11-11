"""A screenshot pinned on screen: a borderless, always-on-top picture.

Wayland does not let apps keep a window above others or place it, so pins
run through XWayland (GDK_BACKEND=x11) with GTK 3, the same way Scrawl
keeps its notes on top. Each pin is its own small process.

  drag            move it
  scroll          zoom in or out
  Ctrl+scroll     change opacity
  double-click    close (or Esc)
  right-click     menu: copy, save, edit, zoom, opacity, close
"""
import os

os.environ['GDK_BACKEND'] = 'x11'

import subprocess  # noqa: E402
import sys  # noqa: E402

import gi  # noqa: E402

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

ZOOMS = [0.25, 0.33, 0.5, 0.67, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0]
BORDER = 1


class Pin(Gtk.Window):
    def __init__(self, path, at, launcher):
        super().__init__(title='Snip Pin')
        self.path = path
        self.launcher = launcher
        self.pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        self.zoom = 1.0
        self.drag = None
        self.moved = False

        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_icon_name('io.github.webhookstudios.Snip')
        self.set_wmclass('snip-pin', 'Snip Pin')
        self.set_app_paintable(True)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual:
            self.set_visual(visual)

        # Big captures start scaled down so they fit on the screen.
        mon = screen.get_display().get_monitor_at_point(*(at or (0, 0))).get_geometry()
        while self.zoom > ZOOMS[0] and (self.pixbuf.get_width() * self.zoom > mon.width * 0.9 or
                                         self.pixbuf.get_height() * self.zoom > mon.height * 0.9):
            self.zoom = ZOOMS[max(0, ZOOMS.index(self.zoom) - 1)]

        self.area = Gtk.DrawingArea()
        self.area.connect('draw', self.on_draw)
        self.add(self.area)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                        Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK |
                        Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.connect('button-press-event', self.on_press)
        self.connect('button-release-event', self.on_release)
        self.connect('motion-notify-event', self.on_motion)
        self.connect('scroll-event', self.on_scroll)
        self.connect('key-press-event', self.on_key)
        self.connect('destroy', Gtk.main_quit)

        self.resize_to_zoom()
        if at:
            # Put the picture back where it was captured (inside the border).
            self.move(at[0] - BORDER, at[1] - BORDER)
        else:
            self.set_position(Gtk.WindowPosition.MOUSE)
        self.show_all()

    def size(self):
        return (round(self.pixbuf.get_width() * self.zoom) + 2 * BORDER,
                round(self.pixbuf.get_height() * self.zoom) + 2 * BORDER)

    def resize_to_zoom(self):
        w, h = self.size()
        self.set_size_request(w, h)
        self.resize(w, h)

    def on_draw(self, _w, cr):
        w, h = self.size()
        cr.set_source_rgba(0.21, 0.52, 0.89, 1)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        cr.save()
        cr.translate(BORDER, BORDER)
        cr.scale(self.zoom, self.zoom)
        Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
        cr.paint()
        cr.restore()
        return True

    # ---- moving -----------------------------------------------------

    def on_press(self, _w, ev):
        if ev.button == 3:
            self.menu().popup_at_pointer(ev)
            return True
        if ev.button != 1:
            return True
        if ev.type == Gdk.EventType._2BUTTON_PRESS:
            self.destroy()
            return True
        x, y = self.get_position()
        self.drag = (ev.x_root - x, ev.y_root - y)
        self.moved = False
        return True

    def on_motion(self, _w, ev):
        if self.drag:
            self.moved = True
            self.move(int(ev.x_root - self.drag[0]), int(ev.y_root - self.drag[1]))
        return True

    def on_release(self, _w, _ev):
        self.drag = None
        return True

    # ---- zoom and opacity --------------------------------------------

    def set_zoom(self, zoom):
        # Zoom around the window's centre so the picture stays put.
        x, y = self.get_position()
        ow, oh = self.size()
        self.zoom = zoom
        nw, nh = self.size()
        self.resize_to_zoom()
        self.move(x + (ow - nw) // 2, y + (oh - nh) // 2)
        self.area.queue_draw()

    def step_zoom(self, direction):
        i = min(range(len(ZOOMS)), key=lambda k: abs(ZOOMS[k] - self.zoom))
        i = max(0, min(len(ZOOMS) - 1, i + direction))
        self.set_zoom(ZOOMS[i])

    def step_opacity(self, direction):
        self.set_opacity(max(0.2, min(1.0, self.get_opacity() + 0.1 * direction)))

    def on_scroll(self, _w, ev):
        direction = 0
        if ev.direction == Gdk.ScrollDirection.UP:
            direction = 1
        elif ev.direction == Gdk.ScrollDirection.DOWN:
            direction = -1
        elif ev.direction == Gdk.ScrollDirection.SMOOTH:
            _ok, _dx, dy = ev.get_scroll_deltas()
            direction = -1 if dy > 0 else 1 if dy < 0 else 0
        if not direction:
            return True
        if ev.state & Gdk.ModifierType.CONTROL_MASK:
            self.step_opacity(direction)
        else:
            self.step_zoom(direction)
        return True

    def on_key(self, _w, ev):
        key = ev.keyval
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        if key == Gdk.KEY_Escape:
            self.destroy()
        elif ctrl and key in (Gdk.KEY_c, Gdk.KEY_C):
            self.copy()
        elif ctrl and key in (Gdk.KEY_s, Gdk.KEY_S):
            self.save_as()
        elif key in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.step_zoom(1)
        elif key in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.step_zoom(-1)
        elif key == Gdk.KEY_0 or key == Gdk.KEY_1:
            self.set_zoom(1.0)
        return True

    # ---- menu ------------------------------------------------------------

    def menu(self):
        menu = Gtk.Menu()

        def item(label, cb):
            mi = Gtk.MenuItem(label=label)
            mi.connect('activate', lambda *_: cb())
            menu.append(mi)

        item('Copy', self.copy)
        item('Save As…', self.save_as)
        item('Edit in Snip', self.edit)
        menu.append(Gtk.SeparatorMenuItem())
        item('Zoom In', lambda: self.step_zoom(1))
        item('Zoom Out', lambda: self.step_zoom(-1))
        item('Actual Size', lambda: self.set_zoom(1.0))
        opacity = Gtk.MenuItem(label='Opacity')
        sub = Gtk.Menu()
        for pct in (100, 80, 60, 40):
            mi = Gtk.CheckMenuItem(label=f'{pct} %')
            mi.set_draw_as_radio(True)
            mi.set_active(round(self.get_opacity() * 100) == pct)
            mi.connect('activate', lambda _m, p=pct: self.set_opacity(p / 100))
            sub.append(mi)
        opacity.set_submenu(sub)
        menu.append(opacity)
        menu.append(Gtk.SeparatorMenuItem())
        item('Close', self.destroy)
        menu.show_all()
        menu.attach_to_widget(self, None)
        return menu

    def copy(self):
        # The app copies so the picture outlives this window (and it uses the
        # shell's clipboard when the Snip extension is on).
        subprocess.Popen([*self.launcher, '--copy', self.path], start_new_session=True)

    def edit(self):
        subprocess.Popen([*self.launcher, '--edit', self.path])

    def save_as(self):
        dialog = Gtk.FileChooserDialog(title='Save Pinned Screenshot', parent=self,
                                       action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons('_Cancel', Gtk.ResponseType.CANCEL, '_Save', Gtk.ResponseType.ACCEPT)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_keep_above(True)
        pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES)
        if pictures:
            dialog.set_current_folder(pictures)
        dialog.set_current_name(GLib.DateTime.new_now_local().format('Snip %Y-%m-%d %H-%M-%S.png'))
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            name = dialog.get_filename()
            kind = 'jpeg' if name.lower().endswith(('.jpg', '.jpeg')) else 'png'
            pb = self.pixbuf
            if kind == 'jpeg' and pb.get_has_alpha():
                pb = pb.composite_color_simple(pb.get_width(), pb.get_height(),
                                               GdkPixbuf.InterpType.NEAREST, 255, 8,
                                               0xffffff, 0xffffff)
            pb.savev(name, kind, ['quality'] if kind == 'jpeg' else [],
                     ['92'] if kind == 'jpeg' else [])
        dialog.destroy()


def main(argv, launcher):
    path = None
    at = None
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == '--at' and args:
            try:
                x, y = (int(v) for v in args.pop(0).split(','))
                at = (x, y)
            except ValueError:
                at = None
        elif not a.startswith('--'):
            path = a
    if not path or not os.path.exists(path):
        print('snip --pin: no picture to pin', file=sys.stderr)
        return 1
    Pin(path, at, launcher)
    Gtk.main()
    return 0


def hold_clipboard(argv):
    """Owns the clipboard through XWayland until something else is copied.

    Without the shell extension this is how Snip copies: an X11 client may
    take the clipboard without having focus (Wayland clients may not), GNOME
    passes it on to Wayland apps, and this little process keeps the picture
    available after the editor closes.
    """
    path = next((a for a in argv if not a.startswith('--')), None)
    if not path or not os.path.exists(path):
        return 1
    pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    started = GLib.get_monotonic_time()

    def owner_change(cb, _event):
        # Our own set_image fires this once; later ones mean someone else copied.
        if GLib.get_monotonic_time() - started > 1_500_000:
            Gtk.main_quit()
    clipboard.set_image(pixbuf)
    clipboard.connect('owner-change', owner_change)
    GLib.timeout_add_seconds(12 * 3600, Gtk.main_quit)
    Gtk.main()
    return 0

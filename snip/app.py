import os
import subprocess

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from . import capture  # noqa: E402
from .config import Config, clean_cache, next_save_path  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

USAGE = '''Usage: snip [OPTION] [FILE...]

  (no option)        open Snip
  --area             capture an area or a window you click
  --window           capture a window
  --screen           capture one screen
  --all              capture all screens
  --delay N          wait N seconds first
  --edit FILE        open a picture in the editor
  --last             open the last capture again
  --copy FILE        copy a picture to the clipboard
  --pin FILE         pin a picture on screen
'''


class SnipApplication(Adw.Application):
    def __init__(self, launcher):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.launcher = launcher
        self.cfg = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        self.cfg = Config()
        clean_cache()
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(os.path.join(HERE, 'icons'))
        from .editor import CSS
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(display, css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, cb in (('preferences', self.show_preferences), ('about', self.show_about),
                         ('quit', self.quit)):
            act = Gio.SimpleAction.new(name, None)
            act.connect('activate', lambda *_a, cb=cb: cb())
            self.add_action(act)
        self.set_accels_for_action('app.quit', ['<Control>q'])

    # ---- command line ---------------------------------------------------

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        cwd = cmdline.get_cwd() or os.getcwd()
        mode = None
        shot_mode = None   # which shortcut made a capture passed with --edit
        delay = None
        origin = None
        edit = []
        copy = None
        i = 0
        while i < len(args):
            a = args[i]
            nxt = args[i + 1] if i + 1 < len(args) else None
            if a in ('--area', '--region'):
                mode = 'area'
            elif a in ('--window', '--screen', '--all'):
                mode = a[2:]
            elif a == '--delay' and nxt is not None:
                delay = _int(nxt, 0)
                i += 1
            elif a == '--origin' and nxt is not None:
                try:
                    origin = tuple(int(v) for v in nxt.split(','))[:2]
                except ValueError:
                    origin = None
                i += 1
            elif a == '--edit' and nxt is not None:
                edit.append(nxt)
                i += 1
            elif a == '--mode' and nxt is not None:
                shot_mode = nxt if nxt in capture.MODES else None
                i += 1
            elif a == '--copy' and nxt is not None:
                copy = nxt
                i += 1
            elif a == '--last':
                if self.cfg['last_capture'] and os.path.exists(self.cfg['last_capture']):
                    edit.append(self.cfg['last_capture'])
            elif a in ('-h', '--help'):
                cmdline.print_literal(USAGE)
                return 0
            elif a == '--version':
                cmdline.print_literal(f'Snip {__version__}\n')
                return 0
            elif a.startswith('--') or a.startswith('-'):
                if a.startswith('--gapplication'):
                    pass
                else:
                    cmdline.printerr_literal(f'snip: unknown option {a}\n\n{USAGE}')
                    return 2
            else:
                edit.append(a if os.path.isabs(a) else os.path.join(cwd, a))
            i += 1

        if copy:
            self.copy_file(os.path.join(cwd, copy))
        for n, path in enumerate(edit):
            if not os.path.exists(path):
                cmdline.printerr_literal(f'snip: no such file: {path}\n')
                continue
            if path.startswith(os.path.join(GLib.get_user_cache_dir(), 'snip', 'captures')) \
                    and os.path.basename(path).startswith('capture-'):
                # A fresh capture from a shortcut or the top-bar menu.
                self.handle_capture(path, origin if n == 0 else None,
                                    shortcut=shot_mode or guess_mode(path))
            else:
                self.open_editor(path, origin if n == 0 else None)
        if mode:
            # From the command line, a dock menu or a custom shortcut.
            self.capture(mode, self.cfg['delay'] if delay is None else delay, shortcut=True)
        if not (copy or edit or mode):
            self.activate()
        return 0

    def do_activate(self):
        from .window import LauncherWindow
        win = next((w for w in self.get_windows() if isinstance(w, LauncherWindow)), None)
        (win or LauncherWindow(self)).present()

    # ---- capture --------------------------------------------------------

    def capture(self, mode, delay=0, monitor=None, on_cancel=None, shortcut=False):
        self.hold()

        def done(path, origin):
            self.release()
            if path:
                self.handle_capture(path, origin, shortcut=mode if shortcut else None)
            elif on_cancel:
                on_cancel()

        def go():
            capture.Capture(self, mode, done, monitor=monitor).start()
            return False
        GLib.timeout_add(max(0, int(delay)) * 1000 + 50, go)

    def handle_capture(self, path, origin, shortcut=None):
        self.cfg['last_capture'] = path
        from .window import LauncherWindow
        for w in self.get_windows():
            if isinstance(w, LauncherWindow) and not w.get_visible():
                w.destroy()
        # Snip's own buttons always open the editor; shortcuts follow the
        # choice made for them in Preferences.
        action = (self.cfg['shortcut_actions'] or {}).get(shortcut, 'editor') \
            if shortcut else 'editor'
        if action == 'copy':
            self.copy_file(path)
            self.notify('Screenshot copied', 'Paste it anywhere.')
        elif action == 'save':
            self.quick_save(path)
        elif action == 'pin':
            self.pin_file(path, origin)
        else:
            self.open_editor(path, origin)

    def quick_save(self, path):
        from .config import desktop_dir
        from .editor import load_surface, write_image
        folder = os.path.expanduser(self.cfg['quick_save_dir'] or desktop_dir())
        try:
            dest = next_save_path(self.cfg, folder=folder)
            write_image(load_surface(path), dest, self.cfg['jpeg_quality'])
        except (OSError, GLib.Error) as e:
            self.notify('Snip could not save the screenshot', str(getattr(e, 'message', e)))
            return
        if self.cfg['copy_after_save']:
            self.copy_file(path)
        self.notify('Screenshot saved', f'{os.path.basename(dest)} in {_tilde(folder)}')

    def open_editor(self, path, origin=None):
        from .editor import EditorWindow
        try:
            win = EditorWindow(self, path, origin)
        except Exception as e:  # noqa: BLE001 - any unreadable picture
            self.notify('Snip could not open the picture', str(e))
            return None
        win.present()
        return win

    def notify(self, title, body=''):
        n = Gio.Notification.new(title)
        if body:
            n.set_body(body)
        self.send_notification(None, n)

    # ---- clipboard and pins -------------------------------------------------

    def copy_file(self, path):
        if capture.shell_set_clipboard(path):
            return
        # Without the extension, a small XWayland helper owns the clipboard
        # (see pin.hold_clipboard). Only one is needed at a time.
        subprocess.run(['pkill', '-f', 'snip.*--hold-clipboar[d]'], check=False)
        subprocess.Popen([*self.launcher, '--hold-clipboard', path],
                         env=_no_gdk_backend(), start_new_session=True)

    def pin_file(self, path, origin=None):
        argv = [*self.launcher, '--pin', path]
        if origin:
            argv += ['--at', f'{origin[0]},{origin[1]}']
        subprocess.Popen(argv, env=_no_gdk_backend(), start_new_session=True)

    # ---- dialogs ----------------------------------------------------------

    def show_preferences(self):
        from .window import PreferencesDialog
        PreferencesDialog(self).present(self.get_active_window())

    def show_about(self):
        about = Adw.AboutDialog(
            application_name='Snip', application_icon=APP_ID, version=__version__,
            developer_name='webhookstudios',
            website='https://github.com/webhookstudios/snip',
            issue_url='https://github.com/webhookstudios/snip/issues',
            license_type=Gtk.License.GPL_2_0,
            comments='Screenshots with arrows, boxes, steps, blur and pins.')
        about.present(self.get_active_window())


def _int(s, default):
    try:
        return int(s)
    except ValueError:
        return default


def _no_gdk_backend():
    env = dict(os.environ)
    env.pop('GDK_BACKEND', None)
    return env


def _tilde(path):
    home = os.path.expanduser('~')
    return '~' + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def guess_mode(path):
    """Which capture a shortcut made, for extensions too old to say: a picture
    the size of every screen or of one screen, else an area or window."""
    try:
        import cairo
        s = cairo.ImageSurface.create_from_png(path)
        size = (s.get_width(), s.get_height())
    except Exception:  # noqa: BLE001
        return 'area'
    monitors = Gdk.Display.get_default().get_monitors()
    geos = [monitors.get_item(i).get_geometry() for i in range(monitors.get_n_items())]
    if geos:
        w = max(g.x + g.width for g in geos) - min(g.x for g in geos)
        h = max(g.y + g.height for g in geos) - min(g.y for g in geos)
        if size == (w, h) and len(geos) > 1:
            return 'all'
        if any(size == (g.width, g.height) for g in geos):
            return 'screen'
    return 'area'

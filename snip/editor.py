"""The annotation editor: one window per screenshot."""
import io
import math
import os
import subprocess

import cairo
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from . import capture  # noqa: E402
from .config import new_cache_path, next_save_path  # noqa: E402
from .shapes import Shape, render  # noqa: E402

# name, label, shortcut key
TOOLS = [
    ('select', 'Select and Move', 'V'),
    ('arrow', 'Arrow', 'A'),
    ('line', 'Line', 'L'),
    ('rect', 'Rectangle', 'R'),
    ('ellipse', 'Ellipse', 'E'),
    ('pen', 'Pen', 'F'),
    ('highlight', 'Highlighter', 'H'),
    ('text', 'Text', 'T'),
    ('step', 'Numbered Step', 'I'),
    ('obscure', 'Blur or Pixelate', 'B'),
    ('crop', 'Crop', 'C'),
]
TWO_POINT = ('arrow', 'line', 'rect', 'ellipse', 'obscure')
FREEHAND = ('pen', 'highlight')

SWATCHES = [
    ('red', (0.90, 0.11, 0.14, 1)),
    ('orange', (1.0, 0.47, 0.0, 1)),
    ('yellow', (1.0, 0.85, 0.0, 1)),
    ('green', (0.18, 0.76, 0.49, 1)),
    ('blue', (0.21, 0.52, 0.89, 1)),
    ('purple', (0.57, 0.25, 0.67, 1)),
    ('black', (0.1, 0.1, 0.1, 1)),
    ('white', (1, 1, 1, 1)),
]
DEFAULT_STYLES = {
    'arrow': {'color': SWATCHES[0][1], 'width': 4},
    'line': {'color': SWATCHES[0][1], 'width': 4},
    'rect': {'color': SWATCHES[0][1], 'width': 3},
    'ellipse': {'color': SWATCHES[0][1], 'width': 3},
    'pen': {'color': SWATCHES[0][1], 'width': 3},
    'highlight': {'color': SWATCHES[2][1], 'width': 4},
    'text': {'color': SWATCHES[0][1], 'width': 5},
    'step': {'color': SWATCHES[0][1], 'width': 3},
    'obscure': {'color': SWATCHES[6][1], 'width': 4, 'mode': 'pixelate'},
}
ZOOMS = [0.1, 0.125, 0.25, 0.33, 0.5, 0.67, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0]

CSS = ''.join(
    f'.swatch-{name} {{ background: rgba({round(c[0] * 255)},{round(c[1] * 255)},'
    f'{round(c[2] * 255)},1); min-width: 18px; min-height: 18px; padding: 0; '
    f'border-radius: 99px; border: 1px solid alpha(currentColor, 0.3); }}\n'
    for name, c in SWATCHES) + '''
.snip-canvas-bg { background-color: alpha(currentColor, 0.06); }
.snip-toolbar { padding: 4px 8px; }
.snip-toolbar togglebutton { min-width: 30px; }
'''


def load_surface(path):
    """Opens a picture as a cairo surface (PNG directly, anything else through GdkPixbuf)."""
    try:
        if path.lower().endswith('.png'):
            return cairo.ImageSurface.create_from_png(path)
    except (cairo.Error, MemoryError):
        pass
    pb = GdkPixbuf.Pixbuf.new_from_file(path)
    pb = pb.apply_embedded_orientation() or pb
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, pb.get_width(), pb.get_height())
    cr = cairo.Context(surf)
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    cr.paint()
    return surf


def surface_to_png_bytes(surface):
    buf = io.BytesIO()
    surface.write_to_png(buf)
    return buf.getvalue()


def write_image(surface, path, quality=90):
    """Saves PNG or JPG depending on the file's extension."""
    if path.lower().endswith(('.jpg', '.jpeg')):
        loader = GdkPixbuf.PixbufLoader.new_with_type('png')
        loader.write(surface_to_png_bytes(surface))
        loader.close()
        pb = loader.get_pixbuf()
        if pb.get_has_alpha():
            pb = pb.composite_color_simple(pb.get_width(), pb.get_height(),
                                           GdkPixbuf.InterpType.NEAREST, 255, 8,
                                           0xffffff, 0xffffff)
        pb.savev(path, 'jpeg', ['quality'], [str(int(quality))])
    else:
        surface.write_to_png(path)


class EditorWindow(Adw.ApplicationWindow):
    def __init__(self, app, path, origin=None):
        super().__init__(application=app, title='Dewdrop')
        self.app = app
        self.cfg = app.cfg
        self.source_path = path
        self.base = load_surface(path)
        self.origin = origin           # screen position of the picture's top-left
        self.shapes = []
        self.undo_stack = []
        self.redo_stack = []
        self.cache = None
        self.current = None
        self.selected = None
        self.editing_index = None      # where a text being edited goes back
        self.crop_rect = None
        self.dirty = False
        self.zoom = 1.0
        self.fit_mode = True
        self.drag_start = None
        self.drag_last = None
        self.move_pushed = False
        self.tool = 'arrow'
        self.styles = {k: dict(v) for k, v in DEFAULT_STYLES.items()}
        for k, v in (self.cfg['styles'] or {}).items():
            if k in self.styles and isinstance(v, dict):
                self.styles[k].update({kk: (tuple(vv) if kk == 'color' else vv)
                                       for kk, vv in v.items()})
        self._syncing = False

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        view.add_top_bar(self._build_header())
        view.add_top_bar(self._build_toolbar())
        view.set_content(self._build_canvas())

        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.on_key)
        self.add_controller(keys)
        self.connect('close-request', self.on_close_request)

        self._initial_size()
        self.set_tool('arrow')
        self.update_title()
        self.sync_history_buttons()

    # ---- building the window ------------------------------------------------

    def _build_header(self):
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title='Dewdrop')
        header.set_title_widget(self.title_widget)

        self.undo_btn = Gtk.Button(icon_name='edit-undo-symbolic', tooltip_text='Undo (Ctrl+Z)')
        self.undo_btn.connect('clicked', lambda *_: self.undo())
        self.redo_btn = Gtk.Button(icon_name='edit-redo-symbolic',
                                   tooltip_text='Redo (Ctrl+Shift+Z)')
        self.redo_btn.connect('clicked', lambda *_: self.redo())
        header.pack_start(self.undo_btn)
        header.pack_start(self.redo_btn)

        menu = Gio.Menu()
        menu.append('Save as…', 'win.save-as')
        menu.append('Open save folder', 'win.open-folder')
        for name, cb in (('save-as', self.save_as), ('open-folder', self.open_folder)):
            act = Gio.SimpleAction.new(name, None)
            act.connect('activate', lambda *_a, cb=cb: cb())
            self.add_action(act)
        save = Adw.SplitButton(label='Save', menu_model=menu,
                               tooltip_text='Save to your screenshots folder (Ctrl+S)')
        save.add_css_class('suggested-action')
        save.connect('clicked', lambda *_: self.save())
        header.pack_end(save)

        copy = Gtk.Button(icon_name='edit-copy-symbolic', tooltip_text='Copy (Ctrl+C)')
        copy.connect('clicked', lambda *_: self.copy())
        header.pack_end(copy)
        pin = Gtk.Button(icon_name='snip-pin-symbolic',
                         tooltip_text='Pin on screen, always on top (Ctrl+P)')
        pin.connect('clicked', lambda *_: self.pin())
        header.pack_end(pin)
        return header

    def _build_toolbar(self):
        bar = Gtk.Box(spacing=4, css_classes=['snip-toolbar'])
        self.tool_buttons = {}
        group = None
        tools = Gtk.Box(spacing=2, css_classes=['linked'])
        for name, label, key in TOOLS:
            b = Gtk.ToggleButton(icon_name=f'snip-{name}-symbolic',
                                 tooltip_text=f'{label} ({key})')
            if group:
                b.set_group(group)
            group = group or b
            b.connect('toggled', lambda btn, n=name: btn.get_active() and self.set_tool(n))
            tools.append(b)
            self.tool_buttons[name] = b
        bar.append(tools)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=6,
                                 margin_end=6))

        # Style: color, swatches, width.
        self.style_box = Gtk.Box(spacing=6)
        self.color_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False),
                                               tooltip_text='Color', valign=Gtk.Align.CENTER)
        self.color_btn.connect('notify::rgba', self.on_color_picked)
        self.style_box.append(self.color_btn)
        sw = Gtk.Box(spacing=3, valign=Gtk.Align.CENTER)
        for name, c in SWATCHES:
            b = Gtk.Button(css_classes=[f'swatch-{name}'], tooltip_text=name.title(),
                           valign=Gtk.Align.CENTER)
            b.connect('clicked', lambda *_a, c=c: self.set_color(c))
            sw.append(b)
        self.style_box.append(sw)
        self.width_label = Gtk.Label(label='Width', margin_start=8)
        self.style_box.append(self.width_label)
        self.width_spin = Gtk.SpinButton.new_with_range(1, 20, 1)
        self.width_spin.set_valign(Gtk.Align.CENTER)
        self.width_spin.connect('value-changed', self.on_width_changed)
        self.style_box.append(self.width_spin)
        self.mode_drop = Gtk.DropDown.new_from_strings(['Pixelate', 'Blur'])
        self.mode_drop.set_valign(Gtk.Align.CENTER)
        self.mode_drop.connect('notify::selected', self.on_mode_changed)
        self.style_box.append(self.mode_drop)
        bar.append(self.style_box)

        # Crop: apply or cancel.
        self.crop_box = Gtk.Box(spacing=6, visible=False)
        self.crop_label = Gtk.Label(label='Drag over the part to keep')
        self.crop_box.append(self.crop_label)
        self.crop_apply = Gtk.Button(label='Crop', css_classes=['suggested-action'],
                                     tooltip_text='Crop (Enter)', sensitive=False)
        self.crop_apply.connect('clicked', lambda *_: self.apply_crop())
        self.crop_box.append(self.crop_apply)
        cancel = Gtk.Button(label='Cancel', tooltip_text='Esc')
        cancel.connect('clicked', lambda *_: self.cancel_crop())
        self.crop_box.append(cancel)
        bar.append(self.crop_box)

        self.delete_btn = Gtk.Button(icon_name='user-trash-symbolic', visible=False,
                                     tooltip_text='Delete (Del)')
        self.delete_btn.connect('clicked', lambda *_: self.delete_selected())
        bar.append(self.delete_btn)

        # Zoom, at the far end.
        spacer = Gtk.Box(hexpand=True)
        bar.append(spacer)
        zoom = Gtk.Box(css_classes=['linked'])
        out = Gtk.Button(icon_name='zoom-out-symbolic', tooltip_text='Zoom out (Ctrl+−)')
        out.connect('clicked', lambda *_: self.step_zoom(-1))
        self.zoom_btn = Gtk.Button(label='100%', tooltip_text='Fit to window (Ctrl+0)')
        self.zoom_btn.connect('clicked', lambda *_: self.fit())
        zin = Gtk.Button(icon_name='zoom-in-symbolic', tooltip_text='Zoom in (Ctrl++)')
        zin.connect('clicked', lambda *_: self.step_zoom(1))
        for b in (out, self.zoom_btn, zin):
            zoom.append(b)
        bar.append(zoom)
        return bar

    def _build_canvas(self):
        self.area = Gtk.DrawingArea(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                    margin_top=16, margin_bottom=16,
                                    margin_start=16, margin_end=16)
        self.area.set_draw_func(self.draw)
        self.scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                           css_classes=['snip-canvas-bg'])
        self.scroller.set_child(self.area)
        self.scroller.get_hadjustment().connect('notify::page-size', self.on_viewport_changed)
        self.scroller.get_vadjustment().connect('notify::page-size', self.on_viewport_changed)

        drag = Gtk.GestureDrag(button=Gdk.BUTTON_PRIMARY)
        drag.connect('drag-begin', self.on_drag_begin)
        drag.connect('drag-update', self.on_drag_update)
        drag.connect('drag-end', self.on_drag_end)
        self.area.add_controller(drag)
        self.drag_gesture = drag

        dbl = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        dbl.connect('pressed', self.on_click)
        self.area.add_controller(dbl)

        motion = Gtk.EventControllerMotion()
        motion.connect('motion', self.on_motion)
        self.area.add_controller(motion)

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        scroll.connect('scroll', self.on_scroll)
        self.scroller.add_controller(scroll)
        return self.scroller

    def _initial_size(self):
        w, h = self.base.get_width(), self.base.get_height()
        mw, mh = 1600, 1000
        monitors = Gdk.Display.get_default().get_monitors()
        if monitors.get_n_items():
            g = monitors.get_item(0).get_geometry()
            mw, mh = g.width, g.height
        chrome_w, chrome_h = 32, 140
        ww = max(760, min(w + chrome_w, int(mw * 0.85)))
        wh = max(480, min(h + chrome_h, int(mh * 0.85)))
        self.set_default_size(ww, wh)
        self.set_zoom(self.fit_zoom(ww - chrome_w, wh - chrome_h), fit=True)

    # ---- zoom -----------------------------------------------------------

    def fit_zoom(self, vw, vh):
        w, h = self.base.get_width(), self.base.get_height()
        return min(1.0, max(0.05, min(vw / w, vh / h)))

    def set_zoom(self, zoom, fit=False):
        self.zoom = zoom
        self.fit_mode = fit
        self.area.set_content_width(max(1, round(self.base.get_width() * zoom)))
        self.area.set_content_height(max(1, round(self.base.get_height() * zoom)))
        self.zoom_btn.set_label(f'{round(zoom * 100)}%')
        self.area.queue_draw()

    def fit(self):
        hp = self.scroller.get_hadjustment().get_page_size()
        vp = self.scroller.get_vadjustment().get_page_size()
        if hp > 0 and vp > 0:
            self.set_zoom(self.fit_zoom(hp - 32, vp - 32), fit=True)

    def on_viewport_changed(self, *_):
        if self.fit_mode:
            GLib.idle_add(lambda: (self.fit_mode and self.fit(), False)[1])

    def step_zoom(self, direction):
        if direction > 0:
            nxt = next((z for z in ZOOMS if z > self.zoom + 1e-6), ZOOMS[-1])
        else:
            nxt = next((z for z in reversed(ZOOMS) if z < self.zoom - 1e-6), ZOOMS[0])
        self.set_zoom(nxt)

    def on_scroll(self, ctl, _dx, dy):
        state = ctl.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK:
            self.step_zoom(-1 if dy > 0 else 1)
            return True
        return False

    # ---- tools and styles -------------------------------------------------

    def set_tool(self, name):
        if self.tool == 'crop' and name != 'crop':
            self.crop_rect = None
        self.finish_text()
        self.tool = name
        btn = self.tool_buttons[name]
        if not btn.get_active():
            btn.set_active(True)
        if name != 'select':
            self.select(None)
        self.crop_box.set_visible(name == 'crop')
        self.style_box.set_visible(name not in ('crop', 'select') or self.selected is not None)
        self.mode_drop.set_visible(name == 'obscure')
        self.width_label.set_label({'text': 'Size', 'step': 'Size',
                                    'obscure': 'Strength'}.get(name, 'Width'))
        if name in self.styles:
            self.show_style(self.styles[name])
        cursor = 'default' if name == 'select' else 'text' if name == 'text' else 'crosshair'
        self.area.set_cursor(Gdk.Cursor.new_from_name(cursor))
        self.area.queue_draw()

    def show_style(self, style):
        self._syncing = True
        r, g, b, a = style['color']
        self.color_btn.set_rgba(Gdk.RGBA(red=r, green=g, blue=b, alpha=a))
        self.width_spin.set_value(style['width'])
        self.mode_drop.set_selected(1 if style.get('mode') == 'blur' else 0)
        self._syncing = False

    def style_target(self):
        """Selected shape's style if there is one, else the tool's."""
        if self.selected is not None:
            return self.shapes[self.selected].kind
        return self.tool if self.tool in self.styles else None

    def change_style(self, **changes):
        kind = self.style_target()
        if not kind:
            return
        self.styles[kind].update(changes)
        self.cfg['styles'] = {k: {kk: list(vv) if kk == 'color' else vv
                                  for kk, vv in v.items()} for k, v in self.styles.items()}
        if self.current is not None and self.current.kind == kind:
            for k, v in changes.items():
                setattr(self.current, k, v)
            self.area.queue_draw()
        if self.selected is not None:
            self.push_undo()
            s = self.shapes[self.selected]
            for k, v in changes.items():
                setattr(s, k, v)
            self.changed()

    def set_color(self, color):
        self.show_style({**self.styles.get(self.style_target() or 'arrow', DEFAULT_STYLES['arrow']),
                         'color': tuple(color)})
        self.change_style(color=tuple(color))

    def on_color_picked(self, btn, _p):
        if self._syncing:
            return
        c = btn.get_rgba()
        self.change_style(color=(c.red, c.green, c.blue, 1.0))

    def on_width_changed(self, spin):
        if not self._syncing:
            self.change_style(width=spin.get_value_as_int())

    def on_mode_changed(self, drop, _p):
        if not self._syncing:
            self.change_style(mode='blur' if drop.get_selected() == 1 else 'pixelate')

    def new_shape(self, kind, points):
        st = self.styles[kind]
        return Shape(kind=kind, color=tuple(st['color']), width=st['width'], points=points,
                     mode=st.get('mode', 'pixelate'))

    # ---- history --------------------------------------------------------

    def snapshot(self):
        return (self.base, [s.copy() for s in self.shapes], self.origin)

    def push_undo(self):
        self.undo_stack.append(self.snapshot())
        if len(self.undo_stack) > 200:
            self.undo_stack.pop(0)
        self.redo_stack.clear()
        self.sync_history_buttons()

    def restore(self, snap):
        base_changed = snap[0] is not self.base
        self.base, shapes, self.origin = snap
        self.shapes = [s.copy() for s in shapes]
        self.select(None)
        if base_changed:
            self.set_zoom(self.zoom, fit=self.fit_mode)
            if self.fit_mode:
                self.fit()
        self.changed()

    def undo(self):
        self.finish_text()
        if self.undo_stack:
            self.redo_stack.append(self.snapshot())
            self.restore(self.undo_stack.pop())
            self.sync_history_buttons()

    def redo(self):
        self.finish_text()
        if self.redo_stack:
            self.undo_stack.append(self.snapshot())
            self.restore(self.redo_stack.pop())
            self.sync_history_buttons()

    def sync_history_buttons(self):
        self.undo_btn.set_sensitive(bool(self.undo_stack))
        self.redo_btn.set_sensitive(bool(self.redo_stack))

    def changed(self):
        self.cache = None
        self.dirty = True
        self.update_title()
        self.area.queue_draw()

    def update_title(self):
        self.title_widget.set_subtitle(f'{self.base.get_width()} × {self.base.get_height()}')

    # ---- selection --------------------------------------------------------

    def select(self, index):
        self.selected = index
        self.delete_btn.set_visible(index is not None)
        if index is not None:
            self.show_style(self.styles.get(self.shapes[index].kind,
                                             DEFAULT_STYLES['arrow']) | {
                'color': self.shapes[index].color, 'width': self.shapes[index].width,
                'mode': self.shapes[index].mode})
            self.mode_drop.set_visible(self.shapes[index].kind == 'obscure')
        self.style_box.set_visible(self.tool not in ('crop', 'select') or index is not None)
        self.area.queue_draw()

    def hit_test(self, x, y):
        tol = 6 / max(self.zoom, 0.1)
        for i in range(len(self.shapes) - 1, -1, -1):
            if self.shapes[i].hit(x, y, tol):
                return i
        return None

    def delete_selected(self):
        if self.selected is None:
            return
        self.push_undo()
        del self.shapes[self.selected]
        self.select(None)
        self.changed()

    def next_step_number(self):
        return max((s.number for s in self.shapes if s.kind == 'step'), default=0) + 1

    # ---- pointer --------------------------------------------------------

    def to_image(self, x, y):
        return x / self.zoom, y / self.zoom

    def on_motion(self, _c, x, y):
        if self.tool == 'select' and not self.drag_start:
            ix, iy = self.to_image(x, y)
            name = 'move' if self.hit_test(ix, iy) is not None else 'default'
            self.area.set_cursor(Gdk.Cursor.new_from_name(name))

    def on_click(self, gesture, n_press, x, y):
        if n_press != 2:
            return
        ix, iy = self.to_image(x, y)
        i = self.hit_test(ix, iy)
        if i is not None and self.shapes[i].kind == 'text':
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.edit_text(i)
        elif self.tool == 'crop' and self.crop_rect:
            self.apply_crop()

    def on_drag_begin(self, _g, x, y):
        self.finish_text()
        ix, iy = self.to_image(x, y)
        self.drag_start = (ix, iy)
        self.drag_last = (ix, iy)
        self.move_pushed = False
        tool = self.tool
        if tool == 'select':
            self.select(self.hit_test(ix, iy))
        elif tool in TWO_POINT:
            self.current = self.new_shape(tool, [(ix, iy), (ix, iy)])
        elif tool in FREEHAND:
            self.current = self.new_shape(tool, [(ix, iy)])
        elif tool == 'step':
            self.push_undo()
            self.move_pushed = True
            s = self.new_shape('step', [(ix, iy)])
            s.number = self.next_step_number()
            self.shapes.append(s)
            self.selected = len(self.shapes) - 1
            self.changed()
        elif tool == 'crop':
            self.crop_rect = (ix, iy, 0, 0)
        self.area.queue_draw()

    def on_drag_update(self, gesture, dx, dy):
        if not self.drag_start:
            return
        sx, sy = self.drag_start
        x, y = sx + dx / self.zoom, sy + dy / self.zoom
        shift = gesture.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK
        tool = self.tool
        if tool in ('select', 'step') and self.selected is not None:
            lx, ly = self.drag_last
            if not self.move_pushed:
                self.push_undo()
                self.move_pushed = True
            self.shapes[self.selected].translate(x - lx, y - ly)
            self.changed()
        elif self.current is not None and tool in TWO_POINT:
            if shift:
                x, y = self.constrain(tool, sx, sy, x, y)
            self.current.points[1] = (x, y)
        elif self.current is not None and tool in FREEHAND:
            if shift:
                self.current.points = [self.current.points[0], (x, y)]
            else:
                px, py = self.current.points[-1]
                if math.hypot(x - px, y - py) >= 1.5 / self.zoom:
                    self.current.points.append((x, y))
        elif tool == 'crop':
            w, h = self.base.get_width(), self.base.get_height()
            x, y = max(0, min(w, x)), max(0, min(h, y))
            ssx, ssy = max(0, min(w, sx)), max(0, min(h, sy))
            self.crop_rect = (min(ssx, x), min(ssy, y), abs(x - ssx), abs(y - ssy))
            self.crop_apply.set_sensitive(self.crop_rect[2] >= 2 and self.crop_rect[3] >= 2)
            self.crop_label.set_label(f'{round(self.crop_rect[2])} × {round(self.crop_rect[3])}')
        self.drag_last = (x, y)
        self.area.queue_draw()

    def constrain(self, tool, sx, sy, x, y):
        dx, dy = x - sx, y - sy
        if tool in ('rect', 'ellipse', 'obscure'):
            d = max(abs(dx), abs(dy))
            return sx + math.copysign(d, dx or 1), sy + math.copysign(d, dy or 1)
        angle = round(math.atan2(dy, dx) / (math.pi / 4)) * (math.pi / 4)
        length = math.hypot(dx, dy)
        return sx + length * math.cos(angle), sy + length * math.sin(angle)

    def on_drag_end(self, _g, dx, dy):
        if not self.drag_start:
            return
        moved = math.hypot(dx, dy) > 3
        start = self.drag_start
        self.drag_start = None
        tool = self.tool
        if self.current is not None:
            shape, self.current = self.current, None
            if not shape.is_empty():
                self.push_undo()
                self.shapes.append(shape)
                self.changed()
        elif tool == 'text' and not moved:
            i = self.hit_test(*start)
            if i is not None and self.shapes[i].kind == 'text':
                self.edit_text(i)
            else:
                self.new_text(start)
        elif tool == 'step':
            self.select(None)
        elif tool == 'crop' and self.crop_rect and not moved:
            self.crop_rect = None
            self.crop_apply.set_sensitive(False)
            self.crop_label.set_label('Drag over the part to keep')
        self.area.queue_draw()

    # ---- text -------------------------------------------------------------

    def new_text(self, pos):
        size = self.styles['text']['width'] * 4 + 8
        shape = self.new_shape('text', [(pos[0], pos[1] - size * 0.6)])
        self.push_undo()
        self.editing_index = len(self.shapes)
        self.open_text_editor(shape)

    def edit_text(self, index):
        self.push_undo()
        shape = self.shapes.pop(index)
        self.editing_index = index
        self.select(None)
        self.cache = None
        self.open_text_editor(shape)

    def open_text_editor(self, shape):
        self.current = shape
        pop = Gtk.Popover(autohide=True, has_arrow=True, position=Gtk.PositionType.BOTTOM)
        pop.set_parent(self.area)
        x, y = shape.points[0]
        rect = Gdk.Rectangle()
        rect.x, rect.y = round(x * self.zoom), round(y * self.zoom)
        rect.width, rect.height = 1, max(1, round(shape.text_size() * 1.2 * self.zoom))
        pop.set_pointing_to(rect)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.NONE, accepts_tab=False,
                          top_margin=6, bottom_margin=6, left_margin=6, right_margin=6)
        tv.get_buffer().set_text(shape.text)
        tv.set_size_request(260, 70)
        frame = Gtk.ScrolledWindow(child=tv, min_content_height=70, max_content_height=200,
                                   propagate_natural_height=True, css_classes=['card'])
        box.append(frame)
        box.append(Gtk.Label(label='Enter to finish · Shift+Enter for a new line',
                             css_classes=['dim-label', 'caption']))
        pop.set_child(box)
        self.text_popover = pop
        self.text_cancelled = False

        def on_text(buf):
            shape.text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
            self.area.queue_draw()
        tv.get_buffer().connect('changed', on_text)

        def on_key(_c, keyval, _code, state):
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and \
                    not state & Gdk.ModifierType.SHIFT_MASK:
                pop.popdown()
                return True
            if keyval == Gdk.KEY_Escape:
                self.text_cancelled = True
                pop.popdown()
                return True
            return False
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', on_key)
        tv.add_controller(keys)
        pop.connect('closed', lambda *_: GLib.idle_add(self._text_closed, pop))
        pop.popup()
        tv.grab_focus()
        self.area.queue_draw()

    def _text_closed(self, pop):
        if self.text_popover is not pop:
            return False
        self.text_popover = None
        shape, self.current = self.current, None
        index = self.editing_index
        self.editing_index = None
        if shape is not None and shape.kind == 'text':
            if self.text_cancelled and index is not None and index < len(self.shapes) + 1 \
                    and self.undo_stack:
                # Esc: put things back the way they were.
                self.restore(self.undo_stack.pop())
                self.sync_history_buttons()
            elif shape.text.strip():
                self.shapes.insert(min(index if index is not None else len(self.shapes),
                                       len(self.shapes)), shape)
                self.changed()
            else:
                self.changed()   # an emptied text is removed
        pop.unparent()
        return False

    def finish_text(self):
        pop = getattr(self, 'text_popover', None)
        if pop is not None:
            pop.popdown()
            self._text_closed(pop)

    # ---- crop -------------------------------------------------------------

    def apply_crop(self):
        if not self.crop_rect:
            return
        x, y, w, h = (round(v) for v in self.crop_rect)
        if w < 2 or h < 2:
            return
        self.push_undo()
        new = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(new)
        cr.set_source_surface(self.base, -x, -y)
        cr.paint()
        self.base = new
        for s in self.shapes:
            s.translate(-x, -y)
        if self.origin:
            self.origin = (self.origin[0] + x, self.origin[1] + y)
        self.crop_rect = None
        self.crop_apply.set_sensitive(False)
        self.crop_label.set_label('Drag over the part to keep')
        self.set_zoom(self.zoom)
        self.fit()
        self.changed()
        self.set_tool('select')

    def cancel_crop(self):
        self.crop_rect = None
        self.set_tool('select')

    # ---- drawing ------------------------------------------------------------

    def committed(self):
        if self.cache is None:
            self.cache = render(self.base, self.shapes)
        return self.cache

    def draw(self, _area, cr, _w, _h):
        z = self.zoom
        cr.save()
        cr.scale(z, z)
        cr.set_source_surface(self.committed(), 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD if z < 1 else
                                   cairo.FILTER_NEAREST if z >= 2 else cairo.FILTER_BILINEAR)
        cr.paint()
        if self.current is not None:
            self.current.draw(cr, None, preview=True)
        cr.restore()

        if self.selected is not None and self.selected < len(self.shapes):
            x, y, w, h = self.shapes[self.selected].bounds()
            cr.set_source_rgba(0.21, 0.52, 0.89, 1)
            cr.set_line_width(1.5)
            cr.set_dash([5, 3])
            cr.rectangle(x * z - 2, y * z - 2, w * z + 4, h * z + 4)
            cr.stroke()
            cr.set_dash([])

        if self.tool == 'crop' and self.crop_rect:
            x, y, w, h = (v * z for v in self.crop_rect)
            W, H = self.base.get_width() * z, self.base.get_height() * z
            cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            cr.rectangle(0, 0, W, H)
            cr.rectangle(x, y, w, h)
            cr.set_source_rgba(0, 0, 0, 0.5)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.set_line_width(1)
            cr.set_dash([4, 4])
            cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
            cr.stroke()

    # ---- keys -------------------------------------------------------------

    def on_key(self, _c, keyval, _code, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        shift = state & Gdk.ModifierType.SHIFT_MASK
        k = Gdk.keyval_to_lower(keyval)
        if ctrl:
            actions = {
                Gdk.KEY_z: self.redo if shift else self.undo,
                Gdk.KEY_y: self.redo,
                Gdk.KEY_c: self.copy,
                Gdk.KEY_s: self.save_as if shift else self.save,
                Gdk.KEY_p: self.pin,
                Gdk.KEY_w: self.close,
                Gdk.KEY_plus: lambda: self.step_zoom(1),
                Gdk.KEY_equal: lambda: self.step_zoom(1),
                Gdk.KEY_KP_Add: lambda: self.step_zoom(1),
                Gdk.KEY_minus: lambda: self.step_zoom(-1),
                Gdk.KEY_KP_Subtract: lambda: self.step_zoom(-1),
                Gdk.KEY_0: self.fit,
                Gdk.KEY_1: lambda: self.set_zoom(1.0),
            }
            if k in actions:
                actions[k]()
                return True
            return False
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_BackSpace) and self.selected is not None:
            self.delete_selected()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.tool == 'crop':
            self.apply_crop()
            return True
        if keyval == Gdk.KEY_Escape:
            if self.tool == 'crop':
                self.cancel_crop()
            elif self.selected is not None:
                self.select(None)
            elif self.tool != 'select':
                self.set_tool('select')
            else:
                self.close()
            return True
        if self.selected is not None and keyval in (Gdk.KEY_Left, Gdk.KEY_Right,
                                                    Gdk.KEY_Up, Gdk.KEY_Down):
            step = 10 if shift else 1
            dx = {Gdk.KEY_Left: -step, Gdk.KEY_Right: step}.get(keyval, 0)
            dy = {Gdk.KEY_Up: -step, Gdk.KEY_Down: step}.get(keyval, 0)
            self.push_undo()
            self.shapes[self.selected].translate(dx, dy)
            self.changed()
            return True
        for name, _label, key in TOOLS:
            if k == Gdk.keyval_from_name(key.lower()) or (name == 'step' and k == Gdk.KEY_n):
                self.set_tool(name)
                return True
        return False

    # ---- output -------------------------------------------------------------

    def flattened(self):
        self.finish_text()
        return render(self.base, self.shapes)

    def toast(self, text, button=None, action=None):
        t = Adw.Toast(title=text, timeout=3)
        if button:
            t.set_button_label(button)
            t.connect('button-clicked', lambda *_: action())
        self.toasts.add_toast(t)

    def copy(self):
        path = new_cache_path('copy')
        self.flattened().write_to_png(path)
        self.app.copy_file(path)
        self.dirty = False
        self.toast('Copied to the clipboard')

    def save(self):
        path = next_save_path(self.cfg)
        self._write(path)

    def _write(self, path):
        try:
            write_image(self.flattened(), path, self.cfg['jpeg_quality'])
        except (GLib.Error, OSError, cairo.Error) as e:
            self.toast(f'Could not save: {getattr(e, "message", None) or e}')
            return
        self.dirty = False
        self.last_saved = path
        if self.cfg['copy_after_save']:
            self.app.copy_file(path) if path.endswith('.png') else None
        self.toast(f'Saved as {os.path.basename(path)}', 'Show', lambda: show_in_files(path))

    def save_as(self):
        dialog = Gtk.FileDialog(title='Save Screenshot')
        folder = os.path.expanduser(self.cfg['save_dir'])
        os.makedirs(folder, exist_ok=True)
        dialog.set_initial_folder(Gio.File.new_for_path(folder))
        dialog.set_initial_name(os.path.basename(next_save_path(self.cfg)))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        for label, pat in (('PNG image', '*.png'), ('JPG image', '*.jpg')):
            f = Gtk.FileFilter(name=label)
            f.add_pattern(pat)
            filters.append(f)
        dialog.set_filters(filters)

        def done(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            path = f.get_path()
            if not path.lower().endswith(('.png', '.jpg', '.jpeg')):
                path += '.jpg' if self.cfg['format'] == 'jpg' else '.png'
            self._write(path)
        dialog.save(self, None, done)

    def open_folder(self):
        folder = os.path.expanduser(self.cfg['save_dir'])
        os.makedirs(folder, exist_ok=True)
        Gtk.FileLauncher.new(Gio.File.new_for_path(folder)).launch(self, None, None)

    def pin(self):
        path = new_cache_path('pin')
        self.flattened().write_to_png(path)
        self.app.pin_file(path, self.origin)
        self.dirty = False
        self.close()

    def on_close_request(self, *_):
        if not self.dirty or not self.shapes and not self.undo_stack:
            return False
        dlg = Adw.AlertDialog(heading='Discard this screenshot?',
                              body='Your markup has not been saved, copied or pinned.')
        dlg.add_response('cancel', 'Cancel')
        dlg.add_response('discard', 'Discard')
        dlg.set_response_appearance('discard', Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response('cancel')

        def resp(_d, r):
            if r == 'discard':
                self.dirty = False
                self.close()
        dlg.connect('response', resp)
        dlg.present(self)
        return True


def show_in_files(path):
    """Opens the file manager with the file selected."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync('org.freedesktop.FileManager1', '/org/freedesktop/FileManager1',
                      'org.freedesktop.FileManager1', 'ShowItems',
                      GLib.Variant('(ass)', ([Gio.File.new_for_path(path).get_uri()], '')),
                      None, Gio.DBusCallFlags.NONE, 3000, None)
    except GLib.Error:
        subprocess.Popen(['xdg-open', os.path.dirname(path)])


__all__ = ['EditorWindow', 'write_image', 'load_surface', 'capture']

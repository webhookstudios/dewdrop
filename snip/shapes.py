"""Annotation objects and how they are drawn with cairo.

Every shape lives in image pixels. The editor keeps a list of them on top
of the captured picture, so they can be moved, recolored and undone until
the picture is exported.
"""
import copy
import math
from dataclasses import dataclass, field

import cairo
import gi

gi.require_version('Pango', '1.0')
gi.require_version('PangoCairo', '1.0')
from gi.repository import Pango, PangoCairo  # noqa: E402

TEXT_FONT = 'Sans Bold'


@dataclass
class Shape:
    kind: str                      # arrow line rect ellipse pen highlight text step obscure
    color: tuple = (0.9, 0.11, 0.14, 1.0)
    width: float = 3
    points: list = field(default_factory=list)   # [(x, y), ...]
    text: str = ''
    number: int = 0
    mode: str = 'pixelate'         # obscure: pixelate or blur

    def copy(self):
        return copy.deepcopy(self)

    # ---- geometry -------------------------------------------------------

    def rect(self):
        """Normalized bounding rectangle of the shape's points: x, y, w, h."""
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)

    def text_size(self):
        return 8 + self.width * 4

    def step_radius(self):
        return 10 + self.width * 3

    def bounds(self):
        """Area the shape covers, for hit testing and selection boxes."""
        if self.kind == 'step':
            r = self.step_radius() + 2
            x, y = self.points[0]
            return x - r, y - r, 2 * r, 2 * r
        if self.kind == 'text':
            w, h = measure_text(self.text or ' ', self.text_size())
            x, y = self.points[0]
            return x - 2, y - 2, w + 4, h + 4
        x, y, w, h = self.rect()
        pad = self.width * (3 if self.kind == 'highlight' else 1) + (4 * self.width + 8
                                                                     if self.kind == 'arrow' else 0)
        return x - pad, y - pad, w + 2 * pad, h + 2 * pad

    def hit(self, px, py, tolerance=6):
        x, y, w, h = self.bounds()
        if not (x - tolerance <= px <= x + w + tolerance and
                y - tolerance <= py <= y + h + tolerance):
            return False
        t = tolerance + self.width
        if self.kind in ('arrow', 'line'):
            return _dist_to_segment(px, py, *self.points[0], *self.points[-1]) <= t
        if self.kind in ('pen', 'highlight'):
            tt = t * (3 if self.kind == 'highlight' else 1)
            pts = self.points
            if len(pts) == 1:
                return math.hypot(px - pts[0][0], py - pts[0][1]) <= tt
            return any(_dist_to_segment(px, py, *pts[i], *pts[i + 1]) <= tt
                       for i in range(len(pts) - 1))
        if self.kind == 'rect':
            rx, ry, rw, rh = self.rect()
            inside = rx + t < px < rx + rw - t and ry + t < py < ry + rh - t
            return not inside
        if self.kind == 'ellipse':
            rx, ry, rw, rh = self.rect()
            if rw < 1 or rh < 1:
                return True
            cx, cy = rx + rw / 2, ry + rh / 2
            d = math.hypot((px - cx) / (rw / 2), (py - cy) / (rh / 2))
            return abs(d - 1) * min(rw, rh) / 2 <= t
        return True  # text, step, obscure: anywhere in the box

    def translate(self, dx, dy):
        self.points = [(x + dx, y + dy) for x, y in self.points]

    def is_empty(self):
        if self.kind in ('text',):
            return not self.text.strip()
        if self.kind in ('step', 'pen', 'highlight'):
            return not self.points
        if len(self.points) < 2:
            return True
        _x, _y, w, h = self.rect()
        return w < 2 and h < 2

    # ---- drawing ----------------------------------------------------------

    def draw(self, cr, target=None, preview=False):
        """Draws the shape. `target` is the image surface being drawn on,
        needed by the obscure tool to read what is underneath."""
        getattr(self, f'_draw_{self.kind}')(cr, target, preview)

    def _stroke_setup(self, cr):
        cr.set_source_rgba(*self.color)
        cr.set_line_width(self.width)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)

    def _draw_line(self, cr, _t, _p):
        (x1, y1), (x2, y2) = self.points[0], self.points[-1]
        self._stroke_setup(cr)
        cr.move_to(x1, y1)
        cr.line_to(x2, y2)
        cr.stroke()

    def _draw_arrow(self, cr, _t, _p):
        (x1, y1), (x2, y2) = self.points[0], self.points[-1]
        angle = math.atan2(y2 - y1, x2 - x1)
        head = 4 * self.width + 8
        length = math.hypot(x2 - x1, y2 - y1)
        head = min(head, length * 0.6) if length else head
        spread = math.radians(26)
        # Stop the shaft inside the head so its round cap doesn't poke out.
        sx = x2 - math.cos(angle) * head * 0.6
        sy = y2 - math.sin(angle) * head * 0.6
        self._stroke_setup(cr)
        cr.move_to(x1, y1)
        cr.line_to(sx, sy)
        cr.stroke()
        cr.move_to(x2, y2)
        cr.line_to(x2 - head * math.cos(angle - spread), y2 - head * math.sin(angle - spread))
        cr.line_to(x2 - head * math.cos(angle + spread), y2 - head * math.sin(angle + spread))
        cr.close_path()
        cr.fill()

    def _draw_rect(self, cr, _t, _p):
        x, y, w, h = self.rect()
        self._stroke_setup(cr)
        cr.rectangle(x, y, w, h)
        cr.stroke()

    def _draw_ellipse(self, cr, _t, _p):
        x, y, w, h = self.rect()
        if w < 1 or h < 1:
            return
        self._stroke_setup(cr)
        cr.save()
        cr.translate(x + w / 2, y + h / 2)
        cr.scale(w / 2, h / 2)
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
        cr.stroke()

    def _path(self, cr):
        pts = self.points
        cr.move_to(*pts[0])
        if len(pts) == 1:
            cr.line_to(pts[0][0] + 0.01, pts[0][1])
        for p in pts[1:]:
            cr.line_to(*p)

    def _draw_pen(self, cr, _t, _p):
        if not self.points:
            return
        self._stroke_setup(cr)
        self._path(cr)
        cr.stroke()

    def _draw_highlight(self, cr, _t, _p):
        if not self.points:
            return
        cr.save()
        cr.set_operator(cairo.OPERATOR_MULTIPLY)
        r, g, b, _a = self.color
        cr.set_source_rgba(r, g, b, 1.0)
        cr.set_line_width(self.width * 6)
        cr.set_line_cap(cairo.LINE_CAP_SQUARE)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        self._path(cr)
        cr.stroke()
        cr.restore()

    def _draw_text(self, cr, _t, _p):
        if not self.text:
            return
        x, y = self.points[0]
        layout = make_layout(cr, self.text, self.text_size())
        cr.save()
        cr.move_to(x, y)
        PangoCairo.layout_path(cr, layout)
        # A thin halo in the opposite brightness keeps text readable anywhere.
        r, g, b, _a = self.color
        light = 0.299 * r + 0.587 * g + 0.114 * b > 0.6
        cr.set_source_rgba(*((0, 0, 0, 0.85) if light else (1, 1, 1, 0.9)))
        cr.set_line_width(max(2, self.text_size() / 8))
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.stroke_preserve()
        cr.set_source_rgba(*self.color)
        cr.fill()
        cr.restore()

    def _draw_step(self, cr, _t, _p):
        x, y = self.points[0]
        r = self.step_radius()
        cr.save()
        cr.arc(x, y + 1.5, r, 0, 2 * math.pi)
        cr.set_source_rgba(0, 0, 0, 0.25)
        cr.fill()
        cr.arc(x, y, r, 0, 2 * math.pi)
        cr.set_source_rgba(*self.color)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 1)
        cr.set_line_width(max(1.5, r / 9))
        cr.stroke()
        rr, gg, bb, _a = self.color
        light = 0.299 * rr + 0.587 * gg + 0.114 * bb > 0.6
        layout = make_layout(cr, str(self.number), r * 1.1)
        w, h = layout.get_pixel_size()
        cr.move_to(x - w / 2, y - h / 2)
        cr.set_source_rgba(*((0.1, 0.1, 0.1, 1) if light else (1, 1, 1, 1)))
        PangoCairo.show_layout(cr, layout)
        cr.restore()

    def obscure_block(self):
        return int(4 + self.width * 2)

    def _draw_obscure(self, cr, target, preview):
        x, y, w, h = (round(v) for v in self.rect())
        if w < 1 or h < 1:
            return
        if target is None or preview:
            cr.save()
            cr.rectangle(x, y, w, h)
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.35)
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.set_line_width(1.5)
            cr.set_dash([6, 4])
            cr.stroke()
            cr.restore()
            return
        target.flush()
        # Copy what is underneath first: we are about to draw over it.
        src = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        c = cairo.Context(src)
        c.set_source_surface(target, -x, -y)
        c.paint()
        block = self.obscure_block()
        if self.mode == 'blur':
            out = blur_surface(src, max(2, block))
            cr.save()
            cr.rectangle(x, y, w, h)
            cr.clip()
            cr.set_source_surface(out, x, y)
            cr.paint()
            cr.restore()
            return
        sw, sh = max(1, math.ceil(w / block)), max(1, math.ceil(h / block))
        small = cairo.ImageSurface(cairo.FORMAT_ARGB32, sw, sh)
        c = cairo.Context(small)
        c.scale(sw / w, sh / h)
        c.set_source_surface(src, 0, 0)
        c.get_source().set_filter(cairo.FILTER_GOOD)
        c.paint()
        cr.save()
        cr.rectangle(x, y, w, h)
        cr.clip()
        cr.translate(x, y)
        cr.scale(w / sw, h / sh)
        cr.set_source_surface(small, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_NEAREST)
        cr.paint()
        cr.restore()


def blur_surface(src, radius):
    """A soft blur made by shrinking the picture and growing it back."""
    w, h = src.get_width(), src.get_height()
    factor = max(2, radius)
    sw, sh = max(1, w // factor), max(1, h // factor)
    small = cairo.ImageSurface(cairo.FORMAT_ARGB32, sw, sh)
    c = cairo.Context(small)
    c.scale(sw / w, sh / h)
    c.set_source_surface(src, 0, 0)
    c.get_source().set_filter(cairo.FILTER_GOOD)
    c.get_source().set_extend(cairo.EXTEND_PAD)
    c.paint()
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    c = cairo.Context(out)
    c.scale(w / sw, h / sh)
    c.set_source_surface(small, 0, 0)
    c.get_source().set_filter(cairo.FILTER_BILINEAR)
    c.get_source().set_extend(cairo.EXTEND_PAD)
    c.paint()
    return out


def make_layout(cr, text, size):
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription.from_string(TEXT_FONT)
    desc.set_absolute_size(size * Pango.SCALE)
    layout.set_font_description(desc)
    layout.set_text(text, -1)
    return layout


_measure = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))


def measure_text(text, size):
    return make_layout(_measure, text, size).get_pixel_size()


def _dist_to_segment(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def render(base, shapes, extra=None):
    """Flattens the picture and its shapes into a new image surface."""
    w, h = base.get_width(), base.get_height()
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(out)
    cr.set_source_surface(base, 0, 0)
    cr.paint()
    for s in shapes:
        s.draw(cr, out)
    if extra:
        extra.draw(cr, out)
    out.flush()
    return out

// Full-screen overlay for picking an area or a window.
// It shows a frozen copy of the screen, so menus and tooltips stay put while
// you choose. Drag for an area; click a window to take just that window.

import Clutter from 'gi://Clutter';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const DRAG_THRESHOLD = 4;
const PICKABLE = [
    Meta.WindowType.NORMAL,
    Meta.WindowType.DIALOG,
    Meta.WindowType.MODAL_DIALOG,
    Meta.WindowType.UTILITY,
];

/**
 * Windows on the active workspace that can be picked, topmost first.
 *
 * @returns {Meta.WindowActor[]}
 */
export function pickableWindows() {
    const ws = global.workspace_manager.get_active_workspace();
    return global.get_window_actors()
        .filter(a => {
            const w = a.meta_window;
            return w && !w.minimized && PICKABLE.includes(w.get_window_type()) &&
                w.showing_on_its_workspace() &&
                (w.is_on_all_workspaces() || w.get_workspace() === ws);
        })
        .reverse();
}

export class Picker {
    /**
     * @param {'area'|'window'} mode
     * @param {Clutter.Content} frozen - the stage content to show underneath
     */
    constructor(mode, frozen) {
        this._mode = mode;
        this._windows = pickableWindows();
        this._start = null;
        this._dragging = false;
        this._hover = null;
        this._rect = null;

        const {width, height} = global.stage;
        this._overlay = new St.Widget({
            reactive: true,
            can_focus: true,
            x: 0, y: 0, width, height,
            style_class: 'dewdrop-overlay',
        });
        this._overlay.add_child(new Clutter.Actor({
            content: frozen, x: 0, y: 0, width, height,
        }));
        this._shades = [0, 1, 2, 3].map(() => {
            const s = new St.Widget({style_class: 'dewdrop-shade'});
            this._overlay.add_child(s);
            return s;
        });
        this._frame = new St.Widget({style_class: 'dewdrop-frame', visible: false});
        this._overlay.add_child(this._frame);
        this._size = new St.Label({style_class: 'dewdrop-size', visible: false});
        this._overlay.add_child(this._size);

        const mon = Main.layoutManager.currentMonitor ?? Main.layoutManager.primaryMonitor;
        this._hint = new St.Label({
            style_class: 'dewdrop-hint',
            text: mode === 'window'
                ? 'Click a window to capture it  ·  Esc to cancel'
                : 'Drag to select an area  ·  Click a window or screen  ·  Esc to cancel',
        });
        this._overlay.add_child(this._hint);
        this._hint.connect('notify::width', () => {
            this._hint.set_position(
                Math.round(mon.x + (mon.width - this._hint.width) / 2),
                mon.y + mon.height - this._hint.height - 48);
        });

        this._overlay.connect('button-press-event', (_a, e) => this._press(e));
        this._overlay.connect('motion-event', (_a, e) => this._motion(e));
        this._overlay.connect('button-release-event', (_a, e) => this._release(e));
        this._overlay.connect('key-press-event', (_a, e) => this._key(e));
    }

    /**
     * Shows the overlay and waits for a choice.
     *
     * @returns {Promise<null|{rect: number[], window: Meta.WindowActor|null}>}
     */
    open() {
        return new Promise(resolve => {
            this._resolve = resolve;
            Main.uiGroup.add_child(this._overlay);
            Main.uiGroup.set_child_above_sibling(this._overlay, null);
            this._grab = Main.pushModal(this._overlay, {actionMode: Shell.ActionMode.POPUP});
            this._overlay.grab_key_focus();
            this._overlay.set_cursor_type(Clutter.CursorType.CROSSHAIR);
            const [x, y] = global.get_pointer();
            this._updateHover(x, y);
            this._layout();
        });
    }

    _finish(result) {
        if (!this._resolve)
            return;
        const resolve = this._resolve;
        this._resolve = null;
        if (this._grab)
            Main.popModal(this._grab);
        this._grab = null;
        this._overlay.destroy();
        resolve(result);
    }

    cancel() {
        this._finish(null);
    }

    _windowAt(x, y) {
        return this._windows.find(a => {
            const r = a.meta_window.get_frame_rect();
            return x >= r.x && y >= r.y && x < r.x + r.width && y < r.y + r.height;
        }) ?? null;
    }

    _monitorAt(x, y) {
        const m = Main.layoutManager.monitors.find(mm =>
            x >= mm.x && y >= mm.y && x < mm.x + mm.width && y < mm.y + mm.height);
        return m ?? Main.layoutManager.primaryMonitor;
    }

    _clip(r) {
        const {width, height} = global.stage;
        const x1 = Math.max(0, r[0]), y1 = Math.max(0, r[1]);
        const x2 = Math.min(width, r[0] + r[2]), y2 = Math.min(height, r[1] + r[3]);
        return [x1, y1, Math.max(0, x2 - x1), Math.max(0, y2 - y1)];
    }

    _updateHover(x, y) {
        this._hover = this._windowAt(x, y);
        if (this._hover) {
            const f = this._hover.meta_window.get_frame_rect();
            this._rect = this._clip([f.x, f.y, f.width, f.height]);
        } else if (this._mode === 'area') {
            const m = this._monitorAt(x, y);
            this._rect = [m.x, m.y, m.width, m.height];
        } else {
            this._rect = null;
        }
    }

    _press(event) {
        if (event.get_button() === Clutter.BUTTON_SECONDARY) {
            this.cancel();
            return Clutter.EVENT_STOP;
        }
        if (event.get_button() !== Clutter.BUTTON_PRIMARY)
            return Clutter.EVENT_STOP;
        const [x, y] = event.get_coords();
        this._start = [Math.round(x), Math.round(y)];
        this._dragging = false;
        return Clutter.EVENT_STOP;
    }

    _motion(event) {
        const [fx, fy] = event.get_coords();
        const x = Math.round(fx), y = Math.round(fy);
        if (this._start && this._mode === 'area') {
            const [sx, sy] = this._start;
            if (!this._dragging &&
                (Math.abs(x - sx) > DRAG_THRESHOLD || Math.abs(y - sy) > DRAG_THRESHOLD))
                this._dragging = true;
            if (this._dragging) {
                this._hover = null;
                this._rect = this._clip([Math.min(sx, x), Math.min(sy, y),
                    Math.abs(x - sx), Math.abs(y - sy)]);
            }
        } else if (!this._start) {
            this._updateHover(x, y);
        }
        this._layout();
        return Clutter.EVENT_STOP;
    }

    _release(event) {
        if (event.get_button() !== Clutter.BUTTON_PRIMARY || !this._start)
            return Clutter.EVENT_STOP;
        const [x, y] = event.get_coords();
        this._start = null;
        if (this._dragging) {
            if (this._rect && this._rect[2] > 1 && this._rect[3] > 1)
                this._finish({rect: this._rect, window: null});
            this._dragging = false;
            return Clutter.EVENT_STOP;
        }
        this._updateHover(Math.round(x), Math.round(y));
        if (this._rect)
            this._finish({rect: this._rect, window: this._hover});
        return Clutter.EVENT_STOP;
    }

    _key(event) {
        const sym = event.get_key_symbol();
        if (sym === Clutter.KEY_Escape) {
            this.cancel();
        } else if ((sym === Clutter.KEY_Return || sym === Clutter.KEY_KP_Enter ||
                    sym === Clutter.KEY_space) && this._rect) {
            this._finish({rect: this._rect, window: this._hover});
        }
        return Clutter.EVENT_STOP;
    }

    _layout() {
        const {width, height} = global.stage;
        const r = this._rect;
        const [s0, s1, s2, s3] = this._shades;
        if (!r) {
            s0.set_position(0, 0);
            s0.set_size(width, height);
            for (const s of [s1, s2, s3])
                s.set_size(0, 0);
            this._frame.hide();
            this._size.hide();
            return;
        }
        const [x, y, w, h] = r;
        s0.set_position(0, 0);
        s0.set_size(width, y);
        s1.set_position(0, y + h);
        s1.set_size(width, Math.max(0, height - y - h));
        s2.set_position(0, y);
        s2.set_size(x, h);
        s3.set_position(x + w, y);
        s3.set_size(Math.max(0, width - x - w), h);

        this._frame.set_position(x - 2, y - 2);
        this._frame.set_size(w + 4, h + 4);
        this._frame.show();

        this._size.text = `${w} × ${h}`;
        this._size.show();
        const ly = y > 30 ? y - this._size.height - 6 : y + 6;
        this._size.set_position(x, ly);
    }
}

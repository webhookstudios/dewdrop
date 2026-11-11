"""Settings, saved as JSON in ~/.config/dewdrop/settings.json."""
import json
import os
import re
import time

from gi.repository import GLib

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), 'dewdrop')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'settings.json')
# Dewdrop was called Snip. Its settings are copied over the first time; the old folder stays as a backup.
OLD_CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), 'snip')
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), 'dewdrop', 'captures')


def desktop_dir():
    return GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP) \
        or os.path.expanduser('~/Desktop')


def default_save_dir():
    pictures = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) \
        or os.path.expanduser('~/Pictures')
    return os.path.join(pictures, 'Screenshots')


DEFAULTS = {
    'save_dir': default_save_dir(),
    'pattern': 'Dewdrop {YYYY}-{MM}-{DD} {hh}-{mm}-{ss}',
    'format': 'png',            # png or jpg
    'jpeg_quality': 90,
    # What a shortcut capture does, per kind: editor, save, copy or pin.
    # Captures started from Dewdrop's own window always open the editor.
    'shortcut_actions': {'area': 'editor', 'window': 'editor',
                         'screen': 'editor', 'all': 'editor'},
    'quick_save_dir': desktop_dir(),
    'copy_after_save': False,
    'delay': 0,
    'styles': {},               # tool -> {"color": [r, g, b, a], "width": n, "mode": ...}
    'last_capture': '',
}

# The default pattern before Dewdrop 1.1.1. Saved settings that still have it get the new default.
OLD_DEFAULT_PATTERN = 'Snip {YYYY}-{MM}-{DD} {hh}-{mm}-{ss}'

PATTERN_HELP = ('{YYYY} year, {MM} month, {DD} day, {hh} hour, {mm} minute, '
                '{ss} second, {n} a number that counts up')


class Config:
    def __init__(self):
        if not os.path.exists(CONFIG_DIR) and os.path.isdir(OLD_CONFIG_DIR):
            import shutil
            shutil.copytree(OLD_CONFIG_DIR, CONFIG_DIR)
        self.data = dict(DEFAULTS)
        try:
            with open(CONFIG_FILE) as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                self.data.update({k: v for k, v in stored.items() if k in DEFAULTS})
                # Older settings had one choice for every capture.
                old = stored.get('after_capture')
                if old in ('save', 'copy', 'pin') and 'shortcut_actions' not in stored:
                    self.data['shortcut_actions'] = dict.fromkeys(('area', 'window', 'screen', 'all'), old)
                self.data['shortcut_actions'] = {**DEFAULTS['shortcut_actions'],
                                                 **(self.data['shortcut_actions'] or {})}
                if self.data['pattern'] == OLD_DEFAULT_PATTERN:
                    self.data['pattern'] = DEFAULTS['pattern']
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, CONFIG_FILE)


def expand_pattern(pattern, when=None, counter=1):
    t = time.localtime(when)
    values = {
        'YYYY': f'{t.tm_year:04d}', 'YY': f'{t.tm_year % 100:02d}',
        'MM': f'{t.tm_mon:02d}', 'DD': f'{t.tm_mday:02d}',
        'hh': f'{t.tm_hour:02d}', 'mm': f'{t.tm_min:02d}', 'ss': f'{t.tm_sec:02d}',
        'n': str(counter),
    }
    name = re.sub(r'\{(\w+)\}', lambda m: values.get(m.group(1), m.group(0)), pattern)
    name = name.replace('/', '-').strip() or 'Snip'
    return name


def next_save_path(cfg, ext=None, folder=None):
    """A file name in the save folder from the pattern that isn't taken yet."""
    ext = ext or ('jpg' if cfg['format'] == 'jpg' else 'png')
    folder = os.path.expanduser(folder or cfg['save_dir'] or default_save_dir())
    os.makedirs(folder, exist_ok=True)
    now = time.time()
    uses_counter = '{n}' in cfg['pattern']
    n = 1
    while True:
        base = expand_pattern(cfg['pattern'], now, n)
        path = os.path.join(folder, f'{base}.{ext}')
        if not uses_counter and n > 1:
            path = os.path.join(folder, f'{base} ({n}).{ext}')
        if not os.path.exists(path):
            return path
        n += 1


def new_cache_path(prefix='capture'):
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    return os.path.join(CACHE_DIR, f'{prefix}-{stamp}-{os.getpid()}-{int(time.time() * 1000) % 1000:03d}.png')


def clean_cache(keep_days=2):
    """Old captures are only needed while they are open, so keep a couple of days."""
    try:
        cutoff = time.time() - keep_days * 86400
        for name in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, name)
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
    except OSError:
        pass

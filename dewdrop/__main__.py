import os
import sys


def launcher():
    """How to start Dewdrop again (for pins and helpers)."""
    return [sys.executable, os.path.realpath(sys.argv[0])]


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    # Pins use GTK 3 on XWayland, so they must start before GTK 4 is loaded.
    if '--pin' in argv[1:]:
        rest = [a for a in argv[1:] if a != '--pin']
        from .pin import main as pin_main
        return pin_main(rest, launcher())
    if '--hold-clipboard' in argv[1:]:
        from .pin import hold_clipboard
        return hold_clipboard([a for a in argv[1:] if a != '--hold-clipboard'])
    from .app import DewdropApplication
    return DewdropApplication(launcher()).run(argv)


if __name__ == '__main__':
    sys.exit(main())

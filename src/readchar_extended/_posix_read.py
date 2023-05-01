from __future__ import annotations
from readchar import config
from readchar import readchar as __readchar

# for posix readchar, see https://manpages.debian.org/bullseye/manpages-dev/termios.3.en.html
def readchar() -> str:
    """Reads a single character from the input stream.
    Blocks until a character is available."""
    return __readchar()

def readkey() -> str:
    """Get a keypress. If an escaped key is pressed, the full sequence is
    read and returned as noted in `_posix_key.py`."""
    c1 = readchar()

    if c1 in config.INTERRUPT_KEYS:
        raise KeyboardInterrupt

    # this decision tree was based on following the known keycodes in _posix_key
    if c1 != '\x1b':
        return c1

    c2 = readchar()
    if c2 not in '\x4f\x5b':
        return ''.join((c1, c2))

    c3 = readchar()
    if c3 not in '\x31\x32\x33\x35\x36\x3b\x3f':
        return ''.join((c1, c2, c3))

    # \x3b: need 2 chars afterwards
    c4 = readchar()
    if c3 != '\x3b' and c4 not in '\x30\x31\x33\x34\x35\x37\x38\x39\x3b':
        return ''.join((c1, c2, c3, c4))

    c5 = readchar()
    if c4 != '\x3b' and c5 not in '\x33\x35\x3b':
        return ''.join((c1, c2, c3, c4, c5))
    
    c6 = readchar()
    if c5 != '\x3b':
        return ''.join((c1, c2, c3, c4, c5, c6))
    
    c7 = readchar()
    return ''.join((c1, c2, c3, c4, c5, c6, c7))

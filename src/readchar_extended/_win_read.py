from __future__ import annotations
from readchar import readchar as __readchar, readkey as __readkey

def readchar() -> str:
    """Reads a single character from the input stream.
    Blocks until a character is available."""
    return __readchar()

def readkey() -> str:
    """Reads the next keypress. If an escaped key is pressed, the full
    sequence is read and returned as noted in `_win_key.py`."""
    # TODO: may need to inject listener somewhere to capture SHIFT_LEFT, SHIFT_RIGHT, etc. since there are no keycodes for it
    return __readkey()

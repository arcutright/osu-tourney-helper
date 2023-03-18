import os

"""
This file is meant to be used as an improved version of `readchar.key`. 

It defines a lot of additional key codes which that module omits.
"""
__GLOBALS_BEFORE_KEYS = [k for k in globals()]

if os.name == 'nt':
    from ._win_key import *
else:
    from ._posix_key import *

# figure out all defined str attributes that don't start with '_' or == 'platform' (defined by readchar::__init__.py)
__globals = globals()
ALL_KEYS = {
    str(v): k for k, v in
    # reversed because later keys overwrite earlier ones
    # this makes it easier to think about, so earlier in the sorted() = will be in the final dict
    reversed(sorted( 
        filter(lambda kv: isinstance(kv[1], str), # only grab str-valued attributes
                [(k, __globals[k]) for k in __globals if k not in __GLOBALS_BEFORE_KEYS
                                                         and k != 'platform'
                                                         and not k.startswith('_')]
        ), key = lambda kv: (
            # the 'not LF/CR' ensures they'll be shown if there are no alternatives, but it's unlikely
            kv[0].count('_') if kv[0] not in ('LF', 'CR') else 2, # ensure 'TAB' comes before equivalents like 'CTRL_I'
            len(kv[0]) if kv[0] not in ('LF', 'CR') else 20, # shorter names have more weight
            kv[0] # fall back to alphabetical
        )
    ))
}
"""All recognized key sequences `sequence`->`attribute name` for the current platform"""

def name(key: str):
    """Try to get the description of a key sequence from `readkey`, if it is known (Note: actually just tries to return what the attribute is named in `key`)"""
    if not key: return key
    return ALL_KEYS.get(key, key)

if os.name == 'nt':
    def is_special(key: str):
        """Check if the output of `readchar.readkey()` is a normal key. For example, [LEFT] = special, [CTRL + A] = special, [A] = not special."""
        return key and (key[0] == '\x00' or key in ALL_KEYS)
else:
    def is_special(key: str):
        """Check if the output of `readchar.readkey()` is a normal key. For example, [LEFT] = special, [CTRL + A] = special, [A] = not special."""
        return key and (key[0] in ('\x00', '\x1b') or key in ALL_KEYS)

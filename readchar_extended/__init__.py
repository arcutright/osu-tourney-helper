__all__ = ["readchar", "readkey", "key", "config", "is_special", "keyname"]

import os
from readchar import config

from . import key
from .key import is_special, name as keyname

if os.name == 'nt':
    from ._win_read import *
else:
    from ._posix_read import *


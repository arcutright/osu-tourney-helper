from readchar.key import *

DEL = DELETE
INS = INSERT

# ref 1 https://msdn.microsoft.com/en-us/library/aa299374
# ref 2 https://www.freepascal.org/docs-html/rtl/keyboard/kbdscancode.html

CTRL_UP    = '\x00\x8D'
CTRL_DOWN  = '\x00\x91'
CTRL_RIGHT = '\x00\x74'
CTRL_LEFT  = '\x00\x73'

CTRL_HOME  = '\x00\x77'
CTRL_END   = '\x00\x75'
CTRL_INS   = '\x00\x04'
CTRL_DEL   = '\x00\x06'
CTRL_TAB   = '\x00\x94'
CTRL_PAGE_UP   = '\x00\x84'
CTRL_PAGE_DOWN = '\x00\x76'

CTRL_BACKSPACE     = '\x7f' # posix backspace
CTRL_ENTER         = '\n' # LF
CTRL_LEFT_BRACKET  = '\x1b' # ESC
CTRL_RIGHT_BRACKET = '\x1d'
CTRL_BACKSLASH     = '\x1c'

CTRL_F1   = '\x00\x5e'
CTRL_F2   = '\x00\x5f'
CTRL_F3   = '\x00\x60'
CTRL_F4   = '\x00\x61'
CTRL_F5   = '\x00\x62'
CTRL_F6   = '\x00\x63'
CTRL_F7   = '\x00\x64'
CTRL_F8   = '\x00\x65'
CTRL_F9   = '\x00\x66'
CTRL_F10  = '\x00\x67'
CTRL_F11  = '\x00\x89'
CTRL_F12  = '\x00\x8a'

ALT_LEFT  = '\x00\x9b'
ALT_RIGHT = '\x00\x9d'
ALT_UP    = '\x00\x98'
ALT_DOWN  = '\x00\xa0'

ALT_HOME  = '\x00\x97'
ALT_END   = '\x00\x9f'
ALT_INS   = '\x00\xA2'
ALT_DEL   = '\x00\xA3'
ALT_TAB   = '\x00\xA5'
ALT_PAGE_UP   = '\x00\x99'
ALT_PAGE_DOWN = '\x00\xA1'

# ALT_LEFT_BRACKET = '['
# ALT_RIGHT_BRACKET = ']'
# ALT_BACKSLASH = '\\'
# ALT_MINUS = '-'
# ALT_EQUALS = '='
# ALT_BACKTICK = '`'

ALT_F1  = '\x00\x68'
ALT_F2  = '\x00\x69'
ALT_F3  = '\x00\x6A'
ALT_F4  = '\x00\x6B'
ALT_F5  = '\x00\x6C'
ALT_F6  = '\x00\x6D'
ALT_F7  = '\x00\x6E'
ALT_F8  = '\x00\x6F'
ALT_F9  = '\x00\x70'
ALT_F10 = '\x00\x71'
ALT_F11 = '\x00\x8B'
ALT_F12 = '\x00\x8C'

# no modifiers show up for alt_[a-z]
# ALT_A = 'a'
# ALT_B = 'b'
# ...

# not possible to capture, there are no codes for these in windows keycode tables...
# SHIFT_LEFT = LEFT
# SHIFT_RIGHT = RIGHT
# SHIFT_UP = UP
# SHIFT_DOWN = DOWN
# SHIFT_HOME  = HOME
# SHIFT_END   = END
# SHIFT_INS = INSERT
# SHIFT_DEL = DELETE
# SHIFT_TAB = TAB
# SHIFT_PAGE_UP   = PAGE_UP
# SHIFT_PAGE_DOWN = PAGE_DOWN

SHIFT_F1  = '\x00\x54'
SHIFT_F2  = '\x00\x55'
SHIFT_F3  = '\x00\x56'
SHIFT_F4  = '\x00\x57'
SHIFT_F5  = '\x00\x58'
SHIFT_F6  = '\x00\x59'
SHIFT_F7  = '\x00\x5a'
SHIFT_F8  = '\x00\x5b'
SHIFT_F9  = '\x00\x5c'
SHIFT_F10 = '\x00\x5d'
SHIFT_F11 = '\x00\x87'
SHIFT_F12 = '\x00\x88'

CTRL_ALT_A = '\x00\x1e'
CTRL_ALT_B = '\x00\x30'
CTRL_ALT_C = '\x00\x2e'
CTRL_ALT_D = '\x00\x20'
CTRL_ALT_E = '\x00\x12'
CTRL_ALT_F = '\x00\x21'
CTRL_ALT_G = '\x00\x22'
CTRL_ALT_H = '\x00\x23'
CTRL_ALT_I = '\x00\x17'
CTRL_ALT_J = '\x00\x24'
CTRL_ALT_K = '\x00\x25'
CTRL_ALT_L = '\x00\x26'
CTRL_ALT_M = '\x00\x32'
CTRL_ALT_N = '\x00\x31'
CTRL_ALT_O = '\x00\x18'
CTRL_ALT_P = '\x00\x19'
CTRL_ALT_Q = '\x00\x10'
CTRL_ALT_R = '\x00\x13'
CTRL_ALT_S = '\x00\x1f'
CTRL_ALT_T = '\x00\x14'
CTRL_ALT_U = '\x00\x16'
CTRL_ALT_V = '\x00\x2f'
CTRL_ALT_W = '\x00\x11'
CTRL_ALT_X = '\x00\x2d'
CTRL_ALT_Y = '\x00\x15'
CTRL_ALT_Z = '\x00\x2c'

CTRL_ALT_1 = '\x00\x78'
CTRL_ALT_2 = '\x00\x79'
CTRL_ALT_3 = '\x00\x7a'
CTRL_ALT_4 = '\x00\x7b'
CTRL_ALT_5 = '\x00\x7c'
CTRL_ALT_6 = '\x00\x7d'
CTRL_ALT_7 = '\x00\x7e'
CTRL_ALT_8 = '\x00\x7f'
CTRL_ALT_9 = '\x00\x80'
CTRL_ALT_0 = '\x00\x81'

# CTRL_ALT_F1 = ALT_F1
# ...

CTRL_ALT_MINUS  = '\x00\x82'
CTRL_ALT_EQUALS = '\x00\x83'

# CTRL_SHIFT_A = CTRL_A
# ...

# ALT_SHIFT_A = 'A'
# ...

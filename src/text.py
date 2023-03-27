import os
import json
from typing import Union, Tuple, Generator, AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.client import HTTPResponse
import urllib.error, urllib.request, urllib.response, urllib.parse
import dateutil.parser
import unicodedata
import re
import ast
import traceback

from helpers import flatten

# optional dependency: pillow
try:
    from PIL import Image, ImageDraw, ImageFont
    from PIL.ImageFont import FreeTypeFont
    __HAS_PILLOW = True
except ImportError:
    @dataclass
    class FreeTypeFont(object):
        """Placeholder for type hints when pillow is not installed"""
        path: str
        size: int = 72
        index: int = 0
    __HAS_PILLOW = False


class Unicode:
    # see https://unicode-explorer.com/b/0300
    @staticmethod
    def _combine(text: str, overlay: str) -> str:
        if not text: return ''
        return overlay.join(str(text)) + overlay
    
    @staticmethod
    def _combine2(text: str, overlay: str, blacklist_chars: 'list[str]') -> str:
        overlay = '\u0332'
        arr = flatten((ch, overlay) if (
                  ch not in blacklist_chars and
                  unicodedata.normalize('NFKD', ch) not in blacklist_chars # catch accented chars
              ) else (ch,) for ch in str(text))
        return ''.join(arr)

    @staticmethod
    def strike(text: str):
        return Unicode._combine(text, '\u0336')
    
    @staticmethod
    def underline(text: str):
        return Unicode._combine(text, '\u0332')
    
    __UNERLINE_OVERLAP_CHARS = set(('p', 'q', 'j', 'g', 'y', ',', '.', '_'))

    @staticmethod
    def underline2(text: str):
        """Underline the text but exclude chars that would interfere ('p', 'j', '_', etc.)"""
        return Unicode._combine2(text, '\u0332', Unicode.__UNERLINE_OVERLAP_CHARS)
    
    @staticmethod
    def double_underline(text: str):
        return Unicode._combine(text, '\u0347')
    
    @staticmethod
    def double_underline2(text: str):
        """Double underline the text but exclude chars that would interfere ('p', 'j', '_', etc.)"""
        return Unicode._combine2(text, '\u0347', Unicode.__UNERLINE_OVERLAP_CHARS)
    
    @staticmethod
    def overline(text: str):
        return Unicode._combine(text, '\u0305')
    
    @staticmethod
    def double_overline(text: str):
        return Unicode._combine(text, '\u033f')

class OsuFontNames:
    """Collection of default chat fonts in osu!"""
    # ref: https://osu.ppy.sh/wiki/en/Client/Options, ctrl+f for 'font'
    STABLE = 'aller'
    """Font used in osu! stable chat"""
    STABLE_OLD = 'tahoma'
    """Font that used to be used in osu! stable chat ('old font' can be chosen in the options)"""
    # ref: https://github.com/ppy/osu/blob/master/osu.Game/Graphics/OsuFont.cs
    LAZER = 'torus'
    """Font used in osu! lazer chat"""
    LAZER_ALT = 'inter' # technically it also uses a customized torus
    """Alternate font used in osu! lazer chat"""
    LAZER_NUMBERS = 'venera'
    """Highly stylized font used sometimes for numbers in osu! lazer"""


def measure_text(text: str, font: 'Union[str, dict[str, int]]') -> int:
    """Get a measurement of some text in a given font (to account for non-monospace fonts). \n
    This uses a 'standardized' font size and only has support for a few fonts. \n
    See `font` caveats in `get_font_measures`
    """
    measures = get_font_measures(font)
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0
    if all(ch in measures for ch in text):
        return sum(measures[ch] for ch in text)
    
    # convert unicode -> ansi equivalents for table lookups (eg: a with accents -> a)
    ptext = plaintext(text)
    # TODO: correct measurements for special chars like Ⓡ Ⓑ ⃝ ⌽ ⍉ ⛝
    # ascii_text = ptext.encode('ascii', 'ignore').decode('ascii', 'ignore').replace('\0', '')
    return sum(measures.get(ch, 'avg') for ch in ptext)

def align_text_amounts(text: str, width: int, direction='left',
                       font: 'Union[str, dict[str, int]]' = OsuFontNames.STABLE
) -> 'Tuple[int, int]':
    """Return the amount of spaces (left, right) needed to pad the given text
    to approximately the given width

    params:
      `text`: text to align
      `width`: width from `measure_text`, not number of chars
      `direction`: 'left', 'right', 'center' (if empty, defaults to 'left')
      `font`: name of the font to use (see caveats in `get_font_measures`), or measures dict
    """
    if not isinstance(text, str):
        text = str(text)
    measures = get_font_measures(font)
    size = measure_text(text, measures)
    space_size = measure_text(' ', measures)
    if width <= 0 or size > width:
        return 0, 0
    n_spaces = int((float(width - size) / space_size) + 0.5)
    # err = width - (size + space_size*n_spaces)
    if n_spaces == 0:
        return 0, 0
    elif not direction or direction == 'left':
        return 0, n_spaces
    elif direction == 'right':
        return n_spaces, 0
    elif direction == 'center':
        if n_spaces < 2:
            return 0, n_spaces
        else:
            n_left = n_spaces // 2
            n_right = n_spaces - n_left
            return n_left, n_right
    else:
        raise NotImplementedError(f"align to direction '{direction}'")

def align_text(text: str, width: int, direction='left',
               font: 'Union[str, dict[str, int]]' = OsuFontNames.STABLE):
    """Return the text approximately aligned to the given width

    params:
      `text`: text to align
      `width`: width from `measure_text`, not number of chars
      `direction`: 'left', 'right', 'center' (if empty, defaults to 'left')
      `font`: name of the font to use (see caveats in `get_font_measures`), or measures dict
    """
    if not isinstance(text, str):
        text = str(text)
    nl, nr = align_text_amounts(text, width, direction, font)
    if nl == nr == 0:
        return text
    else:
        return (' '*nl) + text + (' '*nr)

__osu_link_regex = re.compile(r"\[http[^\] ]+ ([^\]]+)\]")
__markdown_link_regex = re.compile(r"(\[[^\]]+\])\(http[^\(\) ]+\)")
__ansi_escape_regex = re.compile(r"\033\[\d+(?:;\d+)*[a-zA-Z]")
__unicode_escape_regex = re.compile(r"\\u([0-9A-Fa-f]{1,4})")

def __remove_unicode_combining_marks(text: str):
    """Remove unicode combining marks (underline, overline, etc.)"""
    def unicode_match(matchobj: "re.Match"):
        escape_seq = matchobj.group(1)
        ordinal = int(escape_seq, 16)
        # https://unicode-explorer.com/blocks
        if ordinal >= 0x300 and ordinal <= 0x36f: # combining diacritical marks
            return ''
        elif ordinal >= 0x1ab0 and ordinal <= 0x1aff: # combining diacritical marks extended
            return ''
        elif ordinal >= 0x1dc0 and ordinal <= 0x1dff: # combining diacritical marks supplement
            return ''
        elif ordinal >= 0x20d0 and ordinal <= 0x20ff: # combining diacritical marks for symbols
            return ''
        elif ordinal >= 0xfe20 and ordinal <= 0xfe2f: # combining half marks
            return ''
        else:
            char = chr(ordinal)
            return char
    ptext_escaped = text.encode('ascii', 'backslashreplace').decode()
    ptext = __unicode_escape_regex.sub(unicode_match, ptext_escaped)
    return ptext

def plaintext(text: str):
    """Try to get only the meaningful plaintext from some input text. \n
    This tries to removes all escape sequences, control codes, unicode overlays, and
    remove links (`[alias](link)` -> `link`) from markdown/osu links (since only the alias should be shown)
    """
    ptext = str(text)

    # remove control characters
    # \a: terminal bell
    # \b: backspace
    # \v: vertical tab
    # \0: null
    # \177: delete
    ptext = (__ansi_escape_regex.sub('', ptext)
             .replace('\a', '').replace('\b', '').replace('\r', '').replace('\v', '')
             .replace('\0', '').replace('\177', ''))
    
    # convert unicode -> ansi equivalents for table lookups (eg: a with accents -> a)
    ptext = unicodedata.normalize('NFKD', ptext) # or NFC?

    # convert link + alias -> just alias
    ptext = __osu_link_regex.sub(r'\g<1>', ptext)
    ptext = __markdown_link_regex.sub(r'\g<1>', ptext)

    # remove unicode combining marks (underline, overline, etc.)
    ptext = __remove_unicode_combining_marks(ptext)

    # ascii_text = ptext.encode('ascii', 'ignore').decode('ascii', 'ignore') # bad for Ⓡ Ⓑ ⃝ ⌽ ⍉ ⛝
    # ascii2_text = ptext.encode('utf-8', 'ignore').decode('ascii', 'replace') # wrong len for Ⓡ Ⓑ ⃝ ⌽ ⍉ ⛝
    return ptext

def align_table(headers: 'Union[list[str], None]',
                rows: 'list[list[str]]',
                join_text = ' | ',
                directions: 'Union[str, list[str]]' = 'left',
                font: 'Union[str, dict[str, int]]' = OsuFontNames.STABLE,
                accumulate_field_sizes = True
) -> 'Generator[str]':
    """Return a table of headers and rows with text approximately aligned for all fields/headers

    params:
      `headers`: table header (list of each column text)
      `rows`: table row data (each row is a list of column text). Rows can contain links
      such as `[link alias]` or `[alias](link)` and they will still be aligned correctly.
      `join_text`: what chars to put between each aligned column. If this is empty,
      The columns will be stacked tight on the longest row, which is almost certainly
      not what you want.
      `directions`: 'left', 'right', 'center' (provide a list and it will apply across columns,
      eg `directions[0]` for column 0, etc. If empty, defaults to 'left')
      `font`: name of the font to use (see caveats in `get_font_measures`), or measures dict
      `accumulate_field_sizes`: if `False`, tries to align fields on a per-column basis.
      If `True`, tries to align them after accumulating all the aligned columns + join text,
      which may help reduce total alignment error on later columns if the font measures are accurate.
    """
    measures = get_font_measures(font)
    join_size = measure_text(join_text, measures)

    plaintext_headers = [plaintext(h) for h in headers] if headers else []
    max_column_widths = [measure_text(text, measures) for text in plaintext_headers] if headers else []
    
    # plaintext_rows: use regexes to extract labels from formatted links
    # when using link formatting, the link does not contribute width to how it is displayed
    plaintext_rows: 'list[list[str]]' = []
    for r, row in enumerate(rows):
        if isinstance(directions, str):
            directions = [directions] * len(row)
        plain_cols = [plaintext(col) for col in row]
        for c, col in enumerate(plain_cols):
            col_width = measure_text(col, measures)
            if c > len(max_column_widths):
                max_column_widths.append(col_width)
            else:
                max_column_widths[c] = max(max_column_widths[c], col_width)
        plaintext_rows.append(plain_cols)

    max_column_widths[-1] = 0 # no point in trying to align the last field

    if headers:
        header_aligns = [align_text_amounts(text, max_column_widths[c], directions[c], measures) for c, text in enumerate(plaintext_headers)]
        header_text = join_text.join((''.join((nl*' ', text, nr*' ')) for (nl, nr), text in zip(header_aligns, headers)))
        # header_text = join_text.join((align_text(text, max_column_widths[c], directions[c], measures) for c, text in enumerate(headers)))
        yield Unicode.underline2(header_text)

    aligned_fields: 'list[str]' = []
    for r, row in enumerate(rows):
        padding = ''
        text_acc = ''
        size_acc = 0
        aligned_fields.clear()
        for c, col in enumerate(row):
            if not accumulate_field_sizes:
                _, nr = align_text_amounts(plaintext_rows[r][c], max_column_widths[c], directions[c], measures)
            else:
                if c > 0:
                    text_acc += f'{padding}{join_text}'
                    size_acc += join_size
                text_acc += plaintext_rows[r][c]
                size_acc += max_column_widths[c]
                _, nr = align_text_amounts(text_acc, size_acc, directions[c], measures)
            padding = (' '*nr) #; padding_size = space_size*nr
            aligned_fields.append(f'{col}{padding}')

        yield join_text.join(aligned_fields)

def __standardize_font_name(name: str):
    return (
        name.strip().lower()
        .replace('standard', '', 1)
        .replace('regular', '', 1)
        .replace('narrow', 'thin', 1)
        .replace('italics', 'italic', 1)
        .replace(' professional', '', 1).replace(' pro', '', 1)
        .replace('negreta', 'bold', 1).replace('cursiva', 'italic', 1) # have seen this in a few fonts
    ).replace('  ', ' ').strip()

def try_get_font_measures(font: 'Union[str, dict[str, int]]'):
    """ Try to look up character sizes for common fonts like 'arial' or 'tahoma bold'. \n
    This will be a dict of 'char' -> size (int).

    Warning: This supports a very limited number of fonts by default and will raise `NotImplemented`
    if unknown, see `__known_fonts`. You can test ahead of time with `is_font_known`.
    (If your font is missing, you can download the font file (otf, ttf), measure using `measure_new_font`,
    and add it to `__known_fonts`)
    
    params:
      `font`: font name, like 'arial' or 'tahoma bold', or measures dict will no-op.
    """
    if isinstance(font, dict):
        return font
    font = __standardize_font_name(font).replace(' pro', '', 1).replace('  ', ' ').strip()
    measures = __KNOWN_FONTS.get(font, None)
    if measures is not None: return measures

    # apply standard aliases
    for alias, target in __KNOWN_FONT_ALIASES:
        if target not in font and alias in font:
            font = font.replace(alias, target, 1)
    measures = __KNOWN_FONTS.get(font, None)
    if measures is not None: return measures

    # thin ~= light but technically different
    if 'thin' in font:
        font = font.replace('thin', 'light', 1).replace('  ', ' ').strip()
        measures = __KNOWN_FONTS.get(font, None)
    elif 'light' in font:
        font = font.replace('light', 'thin', 1).replace('  ', ' ').strip()
        measures = __KNOWN_FONTS.get(font, None)

    return measures

def get_font_measures(font: 'Union[str, dict[str, int]]'):
    """ Looks up character sizes for common fonts like 'arial' or 'tahoma bold'. \n
    This will be a dict of 'char' -> size (int).

    Warning: This supports a very limited number of fonts by default and will raise `NotImplemented`
    if unknown, see `__known_fonts`. You can test ahead of time with `is_font_known`.
    (If your font is missing, you can download the font file (otf, ttf), measure using `measure_new_font`,
    and add it to `__known_fonts`)
    
    params:
      `font`: font name, like 'arial' or 'tahoma bold', or measures will no-op.
    """
    if isinstance(font, dict):
        return font
    measures = try_get_font_measures(font)
    if measures: return measures

    raise NotImplementedError(
        'Font not supported. To patch in support, you can download the font file (otf, ttf),'
        'measure using `measure_new_font`, and add it to `__known_fonts`'
    )
    # measure_new_font(path)

def is_font_known(font: 'Union[str, dict[str, int]]'):
    """Check if this is a known font. Otherwise `get_font_measures` will raise an exception.
    
    params:
      `font`: font name, like 'arial' or 'tahoma bold', or measures.
    """
    measures = try_get_font_measures(font)
    return measures is not None and len(measures) > 0

def list_known_fonts():
    return [k for k in __KNOWN_FONTS.keys()]

def get_font_short_name(font_or_path: 'Union[FreeTypeFont, str]'):
    """Get the standardized short name of a font (eg: 'arial', 'arial bold', etc.)

    params:
      `font_or_path`: `FreeTypeFont` or path to font file (otf, ttf)
    raises:
      `ImportError`: if pillow is not installed
    """
    if not __HAS_PILLOW:
        raise ImportError("Cannot understand fonts, missing pillow (pip install pillow)")
    
    if isinstance(font_or_path, FreeTypeFont):
        font = font_or_path
    else:
        font_size = 72
        font = ImageFont.truetype(font_or_path, font_size)
    name = ' '.join(font.getname())
    name = __standardize_font_name(name)
    return name

def raw_text_width(text: str, font_or_path: 'Union[FreeTypeFont, str]'):
    """Directly measure the text width in a given font

    params:
      `font_or_path`: `FreeTypeFont` or path to font file (otf, ttf)
    raises:
      `ImportError`: if pillow is not installed
    """
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0
    if not __HAS_PILLOW:
        raise ImportError('Cannot draw fonts, missing pillow (pip install pillow)')
    try:
        # requires pillow
        height = 128
        bg_color = (0,0,0)
        font_color = (255,255,255)
        font_size = 72  # in pixels
        if isinstance(font_or_path, FreeTypeFont):
            font = font_or_path
            font.size = font_size
        else:
            font = ImageFont.truetype(font_or_path, font_size)

        def raw_text_width_inner(itext: str):
            width = len(itext)*120

            # see https://pillow.readthedocs.io/en/stable/reference/ImageDraw.html#PIL.ImageDraw.ImageDraw.textlength
            im = Image.new("RGB", (width, height), bg_color)
            draw = ImageDraw.Draw(im)
            tl0 = draw.textlength(itext, font) # not adjusted for kerning

            # using features= requires libraqm (snag dlls from https://www.lfd.uci.edu/~gohlke/pythonlibs/, ctrl+f for libraqm, copy to venv bin directory)
            # https://learn.microsoft.com/en-us/typography/opentype/spec/featurelist
            #, features=['pwid', 'case', 'clig', 'ss02'])
            
            # im = Image.new("RGB", (width, height), bg_color)
            # draw = ImageDraw.Draw(im)
            # tl1 = draw.textlength(f'{itext}a', font) - draw.textlength('a', font)  # adjusted for kerning
            return tl0
        
        # this technique essentially draws the chars onto a canvas
        # so we have special handling for whitespace chars since they would otherwise result in a blank canvas
        if not (text.startswith((' ', '\t')) or text.endswith((' ', '\t'))):
            w = raw_text_width_inner(text)
            if w is not None and w > 3: # <=3 is pretty unbelievable, seems like a problem
                return w
        
        # fallback when measurements fail (for space chars, ...)
        # the idea is to measure the size of '.{char}.' and subtract '..'
        text2 = text*2
        dots_w = raw_text_width_inner('.a.') - raw_text_width_inner('a') # include kerning/padding space due to .
        w = raw_text_width_inner(f'.{text}.')
        w2 = raw_text_width_inner(f'.{text2}.')
        if w is None:
            bbox = font.getbbox(f'.{text}.')
            if bbox is None:
                raise Exception(f"Failed to measure '{text}' in font '{font_or_path}'")
            w = bbox[2] - bbox[0]
            bbox2 = font.getbbox(f'.{text2}.')
            w2 = bbox2[2] - bbox2[0]
        w -= dots_w
        w2 -= dots_w
        w2 = w2/2
        wavg = (w + w2)/2
        wweight = 0.7 * w2 + 0.3 * w
        return w2
    except Exception as ex:
        print(f"Failed to measure '{text}' in font '{font_or_path}'")
        traceback.print_exc()

def measure_new_fonts(dir: str):
    """Runs `measure_new_font` on every font file (otf, ttf) in dir.
    Does nothing if the font is already known.
    """
    for filename in os.listdir(dir):
        path = os.path.join(dir, filename)
        ext = os.path.splitext(filename)[1].lower()
        if not os.path.isfile(path): continue
        if ext not in ('.otf', '.ttf'): continue
        name = get_font_short_name(path)
        if is_font_known(name): continue
        try:
            measure_new_font(path)
        except Exception as ex:
            print(f"ERROR: Unable to measure font '{path}'")
            traceback.print_exc()


def measure_new_font(font_or_path: 'Union[FreeTypeFont, str]', should_print=True, should_wrap=True):
    """ Calculate the measurements for a given font or file (a dict of char -> size (int)).

    params:
      `font_or_path`: `FreeTypeFont` or path to font file (otf, ttf)
      `should_print`: whether or not to print the resulting dict to `sys.stdout` via `print()`
      `should_wrap`: whether or not to wrap the output when printing (will wrap at approximately keyboard boundaries, eg. \`, q, a, z)
    """
    if isinstance(font_or_path, FreeTypeFont):
        font = font_or_path
    else:
        if isinstance(font_or_path, str) and not os.path.exists(font_or_path):
            name = __standardize_font_name(font_or_path)
            if is_font_known(name):
                return get_font_measures(name)
            else:
                raise FileNotFoundError(f"Cannot open font file '{font_or_path}' or find by name '{name}'")
        if not __HAS_PILLOW:
            raise ImportError('Cannot load fonts, missing pillow (pip install pillow)')
        font = FreeTypeFont(font_or_path, 72)

    chars = (
        '`1234567890-=qwertyuiop[]\\asdfghjkl;\'zxcvbnm,./'
        '~!@#$%^&*()_+QWERTYUIOP{}|ASDFGHJKL:"ZXCVBNM<>?'
        ' \t'
    )
    # chars = ' i`'

    # 32 is arbitrary to try to keep some precision since we're casting to int
    measures = { ch: int(32 * raw_text_width(ch, font) + 0.5) for ch in chars }
    if '\t' in measures:
        # don't average the tab char since it isn't a reliable size every time
        avg = int(float(sum((n for (ch, n) in measures.items() if ch != '\t'))) / (len(measures) - 1))
    else:
        avg = int(float(sum(measures.values())) / len(measures))
    measures['avg'] = avg

    # get a nice formatted dict in your console that can be dropped into the cache below
    font_name = get_font_short_name(font)
    if should_print:
        print(f"'{font_name}': {{")
        output = repr(measures).strip("'")
        i = 1
        for ch in ['q', 'a', 'z', '~', 'Q', 'A', 'Z', ' ']:
            j = output.find(f"'{ch}'", i)
            if j == -1:
                j = i
                break
            print('    ' + output[i:j])
            i = j
        if j < len(output)-1:
            print('    ' + output[j:len(output)-1] + ',')
        print('},')
    # overwrite the dict, mainly for development convenience
    __KNOWN_FONTS[font_name] = measures
    return measures


# these are used for find/replace whenever a font name isn't found in __KNOWN_FONTS
__KNOWN_FONT_ALIASES: "list[Tuple[str, str]]" = [
    ('noto', 'noto sans'),
    ('exo2', 'exo 2'),
    # venera weights are arbitrary choices by me...
    ('venera bold', 'venera 900'),
    ('venera', 'venera 700'),
    ('venera thin', 'venera 500'),
]

__KNOWN_FONTS = {
    # ref: https://osu.ppy.sh/wiki/en/Client/Options, ctrl+f for 'font'
    # ref: https://github.com/ppy/osu-resources/tree/master/osu.Game.Resources/Fonts
    # ref: https://github.com/ppy/osu/blob/master/osu.Game/Graphics/OsuFont.cs

    # aller: current osu! stable font, https://www.fontsquirrel.com/fonts/aller

    # ---------------------------------------------------------------------
    # this whole dict was generated using measure_new_font()
    # ---------------------------------------------------------------------
    # using tl0
    'aller': {
        '`': 1152, '1': 1383, '2': 1383, '3': 1383, '4': 1383, '5': 1383, '6': 1383, '7': 1383, '8': 1383, '9': 1383, '0': 1383, '-': 832, '=': 1383,
        'q': 1348, 'w': 1786, 'e': 1281, 'r': 857, 't': 825, 'y': 1219, 'u': 1323, 'i': 664, 'o': 1330, 'p': 1360, '[': 735, ']': 735, '\\': 949,
        'a': 1191, 's': 1049, 'd': 1350, 'f': 853, 'g': 1274, 'h': 1332, 'j': 668, 'k': 1185, 'l': 687, ';': 599, "'": 514,
        'z': 1099, 'x': 1173, 'c': 1102, 'v': 1224, 'b': 1360, 'n': 1332, 'm': 1968, ',': 491, '.': 556, '/': 945,
        '~': 1099, '!': 668, '@': 2337, '#': 1694, '$': 1383, '%': 2419, '^': 1267, '&': 1754, '*': 1118, '(': 735, ')': 735, '_': 1164, '+': 1383,
        'Q': 1668, 'W': 2122, 'E': 1235, 'R': 1376, 'T': 1212, 'Y': 1364, 'U': 1553, 'I': 641, 'O': 1680, 'P': 1304, '{': 954, '}': 954, '|': 767,
        'A': 1422, 'S': 1256, 'D': 1611, 'F': 1161, 'G': 1569, 'H': 1581, 'J': 913, 'K': 1385, 'L': 1141, ':': 556, '"': 968,
        'Z': 1304, 'X': 1403, 'C': 1424, 'V': 1449, 'B': 1371, 'N': 1567, 'M': 1880, '<': 1383, '>': 1383, '?': 1125,
        ' ': 546, '\t': 1152, 'avg': 1226,
    },
}

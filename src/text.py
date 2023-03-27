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
    # tahoma: old osu! stable font, windows builtin
    # exo 2: osu! lazer? / site (used to be the default for lazer, not sure anymore), https://www.dafont.com/exo-2.font
    # venera: used a lot in lazer's numbers, http://www.losttype.com/font/?name=venera
    # inter: refernced in osu! repos, https://fonts.google.com/specimen/Inter
    # noto sans: refernced in osu! repos, https://fonts.google.com/noto/specimen/Noto+Sans
    # torus, referenced in osu! repos, https://fontsgeek.com/fonts/torus-regular
    #
    # ubuntu: https://fonts.google.com/specimen/Ubuntu
    # arial, times new roman, verdana: common fonts
    # consolas, courier new: common monospace fonts

    # ---------------------------------------------------------------------
    # this whole dict was generated using measure_new_font()
    # ---------------------------------------------------------------------
    # using tl0
    'aller bold': {
        '`': 1152, '1': 1383, '2': 1383, '3': 1383, '4': 1383, '5': 1383, '6': 1383, '7': 1383, '8': 1383, '9': 1383, '0': 1383, '-': 855, '=': 1383, 
        'q': 1353, 'w': 1857, 'e': 1295, 'r': 926, 't': 883, 'y': 1249, 'u': 1318, 'i': 708, 'o': 1337, 'p': 1362, '[': 793, ']': 793, '\\': 954,     
        'a': 1203, 's': 1060, 'd': 1355, 'f': 913, 'g': 1304, 'h': 1350, 'j': 708, 'k': 1233, 'l': 747, ';': 714, "'": 528, 
        'z': 1136, 'x': 1231, 'c': 1095, 'v': 1254, 'b': 1360, 'n': 1353, 'm': 2025, ',': 671, '.': 666, '/': 943, 
        '~': 1097, '!': 689, '@': 2316, '#': 1691, '$': 1383, '%': 2419, '^': 1265, '&': 1758, '*': 1115, '(': 823, ')': 823, '_': 1161, '+': 1383,
        'Q': 1673, 'W': 2122, 'E': 1224, 'R': 1408, 'T': 1307, 'Y': 1417, 'U': 1546, 'I': 661, 'O': 1673, 'P': 1343, '{': 1007, '}': 1007, '|': 786,
        'A': 1466, 'S': 1267, 'D': 1613, 'F': 1168, 'G': 1539, 'H': 1555, 'J': 947, 'K': 1426, 'L': 1136, ':': 666, '"': 989,
        'Z': 1337, 'X': 1459, 'C': 1424, 'V': 1486, 'B': 1394, 'N': 1539, 'M': 1883, '<': 1383, '>': 1383, '?': 1136,
        ' ': 507, '\t': 1152, 'avg': 1249,
    },
    'aller italic': {
        '`': 1152, '1': 1383, '2': 1383, '3': 1383, '4': 1383, '5': 1383, '6': 1383, '7': 1383, '8': 1383, '9': 1383, '0': 1383, '-': 802, '=': 1383,
        'q': 1270, 'w': 1761, 'e': 1148, 'r': 841, 't': 807, 'y': 1152, 'u': 1309, 'i': 657, 'o': 1284, 'p': 1316, '[': 788, ']': 758, '\\': 943,
        'a': 1277, 's': 1000, 'd': 1272, 'f': 774, 'g': 1267, 'h': 1307, 'j': 655, 'k': 1161, 'l': 685, ';': 544, "'": 459,
        'z': 1035, 'x': 1129, 'c': 1058, 'v': 1152, 'b': 1316, 'n': 1325, 'm': 1943, ',': 544, '.': 542, '/': 857,
        '~': 1104, '!': 638, '@': 2277, '#': 1645, '$': 1383, '%': 2272, '^': 1217, '&': 1664, '*': 1097, '(': 763, ')': 779, '_': 1120, '+': 1383,
        'Q': 1615, 'W': 2055, 'E': 1208, 'R': 1325, 'T': 1187, 'Y': 1265, 'U': 1528, 'I': 664, 'O': 1615, 'P': 1304, '{': 876, '}': 867, '|': 767,
        'A': 1380, 'S': 1226, 'D': 1576, 'F': 1143, 'G': 1514, 'H': 1535, 'J': 908, 'K': 1343, 'L': 1129, ':': 542, '"': 897,
        'Z': 1221, 'X': 1399, 'C': 1330, 'V': 1406, 'B': 1334, 'N': 1542, 'M': 1846, '<': 1383, '>': 1383, '?': 1074,
        ' ': 519, '\t': 1095, 'avg': 1197,
    },
    'aller light': {
        '`': 1152, '1': 1383, '2': 1383, '3': 1383, '4': 1383, '5': 1383, '6': 1383, '7': 1383, '8': 1383, '9': 1383, '0': 1383, '-': 814, '=': 1383,
        'q': 1346, 'w': 1733, 'e': 1274, 'r': 807, 't': 781, 'y': 1187, 'u': 1325, 'i': 629, 'o': 1323, 'p': 1357, '[': 694, ']': 694, '\\': 947,
        'a': 1182, 's': 1042, 'd': 1348, 'f': 807, 'g': 1235, 'h': 1318, 'j': 641, 'k': 1157, 'l': 643, ';': 512, "'": 505,
        'z': 1072, 'x': 1129, 'c': 1106, 'v': 1201, 'b': 1360, 'n': 1318, 'm': 1926, ',': 473, '.': 473, '/': 947,
        '~': 1102, '!': 652, '@': 2353, '#': 1696, '$': 1383, '%': 2419, '^': 1270, '&': 1751, '*': 1120, '(': 668, ')': 668, '_': 1166, '+': 1383,
        'Q': 1664, 'W': 2122, 'E': 1244, 'R': 1353, 'T': 1141, 'Y': 1323, 'U': 1558, 'I': 625, 'O': 1664, 'P': 1277, '{': 913, '}': 913, '|': 754,
        'A': 1387, 'S': 1247, 'D': 1611, 'F': 1157, 'G': 1590, 'H': 1599, 'J': 887, 'K': 1367, 'L': 1145, ':': 473, '"': 952,
        'Z': 1279, 'X': 1362, 'C': 1424, 'V': 1422, 'B': 1353, 'N': 1585, 'M': 1878, '<': 1383, '>': 1383, '?': 1118,
        ' ': 576, '\t': 1152, 'avg': 1210,
    },
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
    'arial': {
        '`': 768, '1': 1282, '2': 1282, '3': 1282, '4': 1282, '5': 1282, '6': 1282, '7': 1282, '8': 1282, '9': 1282, '0': 1282, '-': 768, '=': 1346,
        'q': 1282, 'w': 1664, 'e': 1282, 'r': 768, 't': 640, 'y': 1152, 'u': 1282, 'i': 512, 'o': 1282, 'p': 1282, '[': 640, ']': 640, '\\': 640,
        'a': 1282, 's': 1152, 'd': 1282, 'f': 640, 'g': 1282, 'h': 1282, 'j': 512, 'k': 1152, 'l': 512, ';': 640, "'": 440,
        'z': 1152, 'x': 1152, 'c': 1152, 'v': 1152, 'b': 1282, 'n': 1282, 'm': 1920, ',': 640, '.': 640, '/': 640,
        '~': 1346, '!': 640, '@': 2339, '#': 1282, '$': 1282, '%': 2049, '^': 1081, '&': 1537, '*': 897, '(': 768, ')': 768, '_': 1282, '+': 1346,
        'Q': 1792, 'W': 2175, 'E': 1537, 'R': 1664, 'T': 1408, 'Y': 1537, 'U': 1664, 'I': 640, 'O': 1792, 'P': 1537, '{': 770, '}': 770, '|': 599,
        'A': 1537, 'S': 1537, 'D': 1664, 'F': 1408, 'G': 1792, 'H': 1664, 'J': 1152, 'K': 1537, 'L': 1282, ':': 640, '"': 818,
        'Z': 1408, 'X': 1537, 'C': 1664, 'V': 1537, 'B': 1537, 'N': 1664, 'M': 1920, '<': 1346, '>': 1346, '?': 1282,
        ' ': 640, '\t': 1728, 'avg': 1214,
    },
    'arial bold': {
        '`': 768, '1': 1282, '2': 1282, '3': 1282, '4': 1282, '5': 1282, '6': 1282, '7': 1282, '8': 1282, '9': 1282, '0': 1282, '-': 768, '=': 1346,
        'q': 1408, 'w': 1792, 'e': 1282, 'r': 897, 't': 768, 'y': 1282, 'u': 1408, 'i': 640, 'o': 1408, 'p': 1408, '[': 768, ']': 768, '\\': 640,
        'a': 1282, 's': 1282, 'd': 1408, 'f': 768, 'g': 1408, 'h': 1408, 'j': 640, 'k': 1282, 'l': 640, ';': 768, "'": 548,
        'z': 1152, 'x': 1282, 'c': 1282, 'v': 1282, 'b': 1408, 'n': 1408, 'm': 2049, ',': 640, '.': 640, '/': 640,
        '~': 1346, '!': 768, '@': 2247, '#': 1282, '$': 1282, '%': 2049, '^': 1346, '&': 1664, '*': 897, '(': 768, ')': 768, '_': 1282, '+': 1346,
        'Q': 1792, 'W': 2175, 'E': 1537, 'R': 1664, 'T': 1408, 'Y': 1537, 'U': 1664, 'I': 640, 'O': 1792, 'P': 1537, '{': 897, '}': 897, '|': 645,
        'A': 1664, 'S': 1537, 'D': 1664, 'F': 1408, 'G': 1792, 'H': 1664, 'J': 1282, 'K': 1664, 'L': 1408, ':': 768, '"': 1093,
        'Z': 1408, 'X': 1537, 'C': 1664, 'V': 1537, 'B': 1664, 'N': 1664, 'M': 1920, '<': 1346, '>': 1346, '?': 1408,
        ' ': 640, '\t': 1728, 'avg': 1270,
    },
    'arial italic': {
        '`': 768, '1': 1282, '2': 1282, '3': 1282, '4': 1282, '5': 1282, '6': 1282, '7': 1282, '8': 1282, '9': 1282, '0': 1282, '-': 768, '=': 1346,
        'q': 1282, 'w': 1664, 'e': 1282, 'r': 768, 't': 640, 'y': 1152, 'u': 1282, 'i': 512, 'o': 1282, 'p': 1282, '[': 640, ']': 640, '\\': 640,
        'a': 1282, 's': 1152, 'd': 1282, 'f': 640, 'g': 1282, 'h': 1282, 'j': 512, 'k': 1152, 'l': 512, ';': 640, "'": 440,
        'z': 1152, 'x': 1152, 'c': 1152, 'v': 1152, 'b': 1282, 'n': 1282, 'm': 1920, ',': 640, '.': 640, '/': 640,
        '~': 1346, '!': 640, '@': 2339, '#': 1282, '$': 1282, '%': 2049, '^': 1081, '&': 1537, '*': 897, '(': 768, ')': 768, '_': 1282, '+': 1346,
        'Q': 1792, 'W': 2175, 'E': 1537, 'R': 1664, 'T': 1408, 'Y': 1537, 'U': 1664, 'I': 640, 'O': 1792, 'P': 1537, '{': 770, '}': 770, '|': 599,
        'A': 1537, 'S': 1537, 'D': 1664, 'F': 1408, 'G': 1792, 'H': 1664, 'J': 1152, 'K': 1537, 'L': 1282, ':': 640, '"': 818,
        'Z': 1408, 'X': 1537, 'C': 1664, 'V': 1537, 'B': 1537, 'N': 1664, 'M': 1920, '<': 1346, '>': 1346, '?': 1282,
        ' ': 640, '\t': 1728, 'avg': 1214,
    },
    'arial thin': {
        '`': 629, '1': 1051, '2': 1051, '3': 1051, '4': 1051, '5': 1051, '6': 1051, '7': 1051, '8': 1051, '9': 1051, '0': 1051, '-': 629, '=': 1104,
        'q': 1051, 'w': 1364, 'e': 1051, 'r': 629, 't': 526, 'y': 945, 'u': 1051, 'i': 420, 'o': 1051, 'p': 1051, '[': 526, ']': 526, '\\': 526,
        'a': 1051, 's': 945, 'd': 1051, 'f': 526, 'g': 1051, 'h': 1051, 'j': 420, 'k': 945, 'l': 420, ';': 526, "'": 363,
        'z': 945, 'x': 945, 'c': 945, 'v': 945, 'b': 1051, 'n': 1051, 'm': 1574, ',': 526, '.': 526, '/': 526,
        '~': 1104, '!': 526, '@': 1917, '#': 1051, '$': 1051, '%': 1680, '^': 887, '&': 1260, '*': 735, '(': 629, ')': 629, '_': 1051, '+': 1104,
        'Q': 1471, 'W': 1783, 'E': 1260, 'R': 1364, 'T': 1155, 'Y': 1260, 'U': 1364, 'I': 526, 'O': 1471, 'P': 1260, '{': 631, '}': 631, '|': 491,
        'A': 1260, 'S': 1260, 'D': 1364, 'F': 1155, 'G': 1471, 'H': 1364, 'J': 945, 'K': 1260, 'L': 1051, ':': 526, '"': 671,
        'Z': 1155, 'X': 1260, 'C': 1364, 'V': 1260, 'B': 1260, 'N': 1364, 'M': 1574, '<': 1104, '>': 1104, '?': 1051,
        ' ': 526, '\t': 526, 'avg': 995,
    },
    'consolas': {
        '`': 1267, '1': 1267, '2': 1267, '3': 1267, '4': 1267, '5': 1267, '6': 1267, '7': 1267, '8': 1267, '9': 1267, '0': 1267, '-': 1267, '=': 1267,
        'q': 1267, 'w': 1267, 'e': 1267, 'r': 1267, 't': 1267, 'y': 1267, 'u': 1267, 'i': 1267, 'o': 1267, 'p': 1267, '[': 1267, ']': 1267, '\\': 1267,
        'a': 1267, 's': 1267, 'd': 1267, 'f': 1267, 'g': 1267, 'h': 1267, 'j': 1267, 'k': 1267, 'l': 1267, ';': 1267, "'": 1267,
        'z': 1267, 'x': 1267, 'c': 1267, 'v': 1267, 'b': 1267, 'n': 1267, 'm': 1267, ',': 1267, '.': 1267, '/': 1267,
        '~': 1267, '!': 1267, '@': 1267, '#': 1267, '$': 1267, '%': 1267, '^': 1267, '&': 1267, '*': 1267, '(': 1267, ')': 1267, '_': 1267, '+': 1267,
        'Q': 1267, 'W': 1267, 'E': 1267, 'R': 1267, 'T': 1267, 'Y': 1267, 'U': 1267, 'I': 1267, 'O': 1267, 'P': 1267, '{': 1267, '}': 1267, '|': 1267,
        'A': 1267, 'S': 1267, 'D': 1267, 'F': 1267, 'G': 1267, 'H': 1267, 'J': 1267, 'K': 1267, 'L': 1267, ':': 1267, '"': 1267,
        'Z': 1267, 'X': 1267, 'C': 1267, 'V': 1267, 'B': 1267, 'N': 1267, 'M': 1267, '<': 1267, '>': 1267, '?': 1267,
        ' ': 1267, '\t': 1267, 'avg': 1267,
    },
    'consolas bold': {
        '`': 1267, '1': 1267, '2': 1267, '3': 1267, '4': 1267, '5': 1267, '6': 1267, '7': 1267, '8': 1267, '9': 1267, '0': 1267, '-': 1267, '=': 1267,
        'q': 1267, 'w': 1267, 'e': 1267, 'r': 1267, 't': 1267, 'y': 1267, 'u': 1267, 'i': 1267, 'o': 1267, 'p': 1267, '[': 1267, ']': 1267, '\\': 1267,
        'a': 1267, 's': 1267, 'd': 1267, 'f': 1267, 'g': 1267, 'h': 1267, 'j': 1267, 'k': 1267, 'l': 1267, ';': 1267, "'": 1267,
        'z': 1267, 'x': 1267, 'c': 1267, 'v': 1267, 'b': 1267, 'n': 1267, 'm': 1267, ',': 1267, '.': 1267, '/': 1267,
        '~': 1267, '!': 1267, '@': 1267, '#': 1267, '$': 1267, '%': 1267, '^': 1267, '&': 1267, '*': 1267, '(': 1267, ')': 1267, '_': 1267, '+': 1267,
        'Q': 1267, 'W': 1267, 'E': 1267, 'R': 1267, 'T': 1267, 'Y': 1267, 'U': 1267, 'I': 1267, 'O': 1267, 'P': 1267, '{': 1267, '}': 1267, '|': 1267,
        'A': 1267, 'S': 1267, 'D': 1267, 'F': 1267, 'G': 1267, 'H': 1267, 'J': 1267, 'K': 1267, 'L': 1267, ':': 1267, '"': 1267,
        'Z': 1267, 'X': 1267, 'C': 1267, 'V': 1267, 'B': 1267, 'N': 1267, 'M': 1267, '<': 1267, '>': 1267, '?': 1267,
        ' ': 1267, '\t': 1267, 'avg': 1267,
    },
    'consolas italic': {
        '`': 1267, '1': 1267, '2': 1267, '3': 1267, '4': 1267, '5': 1267, '6': 1267, '7': 1267, '8': 1267, '9': 1267, '0': 1267, '-': 1267, '=': 1267,
        'q': 1267, 'w': 1267, 'e': 1267, 'r': 1267, 't': 1267, 'y': 1267, 'u': 1267, 'i': 1267, 'o': 1267, 'p': 1267, '[': 1267, ']': 1267, '\\': 1267,
        'a': 1267, 's': 1267, 'd': 1267, 'f': 1267, 'g': 1267, 'h': 1267, 'j': 1267, 'k': 1267, 'l': 1267, ';': 1267, "'": 1267,
        'z': 1267, 'x': 1267, 'c': 1267, 'v': 1267, 'b': 1267, 'n': 1267, 'm': 1267, ',': 1267, '.': 1267, '/': 1267,
        '~': 1267, '!': 1267, '@': 1267, '#': 1267, '$': 1267, '%': 1267, '^': 1267, '&': 1267, '*': 1267, '(': 1267, ')': 1267, '_': 1267, '+': 1267,
        'Q': 1267, 'W': 1267, 'E': 1267, 'R': 1267, 'T': 1267, 'Y': 1267, 'U': 1267, 'I': 1267, 'O': 1267, 'P': 1267, '{': 1267, '}': 1267, '|': 1267,
        'A': 1267, 'S': 1267, 'D': 1267, 'F': 1267, 'G': 1267, 'H': 1267, 'J': 1267, 'K': 1267, 'L': 1267, ':': 1267, '"': 1267,
        'Z': 1267, 'X': 1267, 'C': 1267, 'V': 1267, 'B': 1267, 'N': 1267, 'M': 1267, '<': 1267, '>': 1267, '?': 1267,
        ' ': 1267, '\t': 1267, 'avg': 1267,
    },
    'courier new': {
        '`': 1383, '1': 1383, '2': 1383, '3': 1383, '4': 1383, '5': 1383, '6': 1383, '7': 1383, '8': 1383, '9': 1383, '0': 1383, '-': 1383, '=': 1383,
        'q': 1383, 'w': 1383, 'e': 1383, 'r': 1383, 't': 1383, 'y': 1383, 'u': 1383, 'i': 1383, 'o': 1383, 'p': 1383, '[': 1383, ']': 1383, '\\': 1383,
        'a': 1383, 's': 1383, 'd': 1383, 'f': 1383, 'g': 1383, 'h': 1383, 'j': 1383, 'k': 1383, 'l': 1383, ';': 1383, "'": 1383,
        'z': 1383, 'x': 1383, 'c': 1383, 'v': 1383, 'b': 1383, 'n': 1383, 'm': 1383, ',': 1383, '.': 1383, '/': 1383,
        '~': 1383, '!': 1383, '@': 1383, '#': 1383, '$': 1383, '%': 1383, '^': 1383, '&': 1383, '*': 1383, '(': 1383, ')': 1383, '_': 1383, '+': 1383,
        'Q': 1383, 'W': 1383, 'E': 1383, 'R': 1383, 'T': 1383, 'Y': 1383, 'U': 1383, 'I': 1383, 'O': 1383, 'P': 1383, '{': 1383, '}': 1383, '|': 1383,
        'A': 1383, 'S': 1383, 'D': 1383, 'F': 1383, 'G': 1383, 'H': 1383, 'J': 1383, 'K': 1383, 'L': 1383, ':': 1383, '"': 1383,
        'Z': 1383, 'X': 1383, 'C': 1383, 'V': 1383, 'B': 1383, 'N': 1383, 'M': 1383, '<': 1383, '>': 1383, '?': 1383,
        ' ': 1383, '\t': 1383, 'avg': 1383,
    },
    'courier new bold': {
        '`': 1383, '1': 1383, '2': 1383, '3': 1383, '4': 1383, '5': 1383, '6': 1383, '7': 1383, '8': 1383, '9': 1383, '0': 1383, '-': 1383, '=': 1383,
        'q': 1383, 'w': 1383, 'e': 1383, 'r': 1383, 't': 1383, 'y': 1383, 'u': 1383, 'i': 1383, 'o': 1383, 'p': 1383, '[': 1383, ']': 1383, '\\': 1383,
        'a': 1383, 's': 1383, 'd': 1383, 'f': 1383, 'g': 1383, 'h': 1383, 'j': 1383, 'k': 1383, 'l': 1383, ';': 1383, "'": 1383,
        'z': 1383, 'x': 1383, 'c': 1383, 'v': 1383, 'b': 1383, 'n': 1383, 'm': 1383, ',': 1383, '.': 1383, '/': 1383,
        '~': 1383, '!': 1383, '@': 1383, '#': 1383, '$': 1383, '%': 1383, '^': 1383, '&': 1383, '*': 1383, '(': 1383, ')': 1383, '_': 1383, '+': 1383,
        'Q': 1383, 'W': 1383, 'E': 1383, 'R': 1383, 'T': 1383, 'Y': 1383, 'U': 1383, 'I': 1383, 'O': 1383, 'P': 1383, '{': 1383, '}': 1383, '|': 1383,
        'A': 1383, 'S': 1383, 'D': 1383, 'F': 1383, 'G': 1383, 'H': 1383, 'J': 1383, 'K': 1383, 'L': 1383, ':': 1383, '"': 1383,
        'Z': 1383, 'X': 1383, 'C': 1383, 'V': 1383, 'B': 1383, 'N': 1383, 'M': 1383, '<': 1383, '>': 1383, '?': 1383,
        ' ': 1383, '\t': 1383, 'avg': 1383,
    },
    'courier new italic': {
        '`': 1383, '1': 1383, '2': 1383, '3': 1383, '4': 1383, '5': 1383, '6': 1383, '7': 1383, '8': 1383, '9': 1383, '0': 1383, '-': 1383, '=': 1383,
        'q': 1383, 'w': 1383, 'e': 1383, 'r': 1383, 't': 1383, 'y': 1383, 'u': 1383, 'i': 1383, 'o': 1383, 'p': 1383, '[': 1383, ']': 1383, '\\': 1383,
        'a': 1383, 's': 1383, 'd': 1383, 'f': 1383, 'g': 1383, 'h': 1383, 'j': 1383, 'k': 1383, 'l': 1383, ';': 1383, "'": 1383,
        'z': 1383, 'x': 1383, 'c': 1383, 'v': 1383, 'b': 1383, 'n': 1383, 'm': 1383, ',': 1383, '.': 1383, '/': 1383,
        '~': 1383, '!': 1383, '@': 1383, '#': 1383, '$': 1383, '%': 1383, '^': 1383, '&': 1383, '*': 1383, '(': 1383, ')': 1383, '_': 1383, '+': 1383,
        'Q': 1383, 'W': 1383, 'E': 1383, 'R': 1383, 'T': 1383, 'Y': 1383, 'U': 1383, 'I': 1383, 'O': 1383, 'P': 1383, '{': 1383, '}': 1383, '|': 1383,
        'A': 1383, 'S': 1383, 'D': 1383, 'F': 1383, 'G': 1383, 'H': 1383, 'J': 1383, 'K': 1383, 'L': 1383, ':': 1383, '"': 1383,
        'Z': 1383, 'X': 1383, 'C': 1383, 'V': 1383, 'B': 1383, 'N': 1383, 'M': 1383, '<': 1383, '>': 1383, '?': 1383,
        ' ': 1383, '\t': 1383, 'avg': 1383,
    },
    'exo 2 bold': {
        '`': 814, '1': 989, '2': 1332, '3': 1304, '4': 1466, '5': 1270, '6': 1355, '7': 1212, '8': 1426, '9': 1355, '0': 1449, '-': 954, '=': 1334,
        'q': 1362, 'w': 1952, 'e': 1290, 'r': 979, 't': 947, 'y': 1295, 'u': 1360, 'i': 636, 'o': 1353, 'p': 1383, '[': 797, ']': 797, '\\': 1270,
        'a': 1307, 's': 1214, 'd': 1369, 'f': 931, 'g': 1334, 'h': 1380, 'j': 641, 'k': 1284, 'l': 754, ';': 597, "'": 491,
        'z': 1210, 'x': 1300, 'c': 1168, 'v': 1297, 'b': 1364, 'n': 1380, 'm': 2023, ',': 560, '.': 567, '/': 1267,
        '~': 1182, '!': 691, '@': 1742, '#': 1636, '$': 1330, '%': 2090, '^': 1065, '&': 1763, '*': 1042, '(': 841, ')': 841, '_': 1284, '+': 1242,
        'Q': 1581, 'W': 2242, 'E': 1327, 'R': 1482, 'T': 1385, 'Y': 1401, 'U': 1569, 'I': 673, 'O': 1576, 'P': 1415, '{': 781, '}': 784, '|': 602,
        'A': 1498, 'S': 1341, 'D': 1567, 'F': 1277, 'G': 1486, 'H': 1585, 'J': 850, 'K': 1454, 'L': 1226, ':': 597, '"': 864,
        'Z': 1343, 'X': 1477, 'C': 1350, 'V': 1466, 'B': 1468, 'N': 1664, 'M': 2099, '<': 1194, '>': 1191, '?': 1189,
        ' ': 500, '\t': 1383, 'avg': 1235,
    },
    'exo 2 italic': {
        '`': 724, '1': 910, '2': 1284, '3': 1267, '4': 1406, '5': 1228, '6': 1311, '7': 1175, '8': 1396, '9': 1311, '0': 1385, '-': 1030, '=': 1350,
        'q': 1314, 'w': 1959, 'e': 1237, 'r': 917, 't': 869, 'y': 1242, 'u': 1307, 'i': 567, 'o': 1300, 'p': 1325, '[': 749, ']': 749, '\\': 1171,
        'a': 1320, 's': 1166, 'd': 1318, 'f': 869, 'g': 1325, 'h': 1332, 'j': 567, 'k': 1201, 'l': 675, ';': 551, "'": 475,
        'z': 1161, 'x': 1212, 'c': 1115, 'v': 1224, 'b': 1320, 'n': 1332, 'm': 2009, ',': 523, '.': 519, '/': 1208,
        '~': 1203, '!': 625, '@': 1779, '#': 1572, '$': 1300, '%': 2060, '^': 982, '&': 1740, '*': 1032, '(': 820, ')': 820, '_': 1339, '+': 1261,
        'Q': 1535, 'W': 2173, 'E': 1286, 'R': 1424, 'T': 1320, 'Y': 1302, 'U': 1530, 'I': 602, 'O': 1535, 'P': 1355, '{': 733, '}': 735, '|': 565,
        'A': 1426, 'S': 1290, 'D': 1525, 'F': 1237, 'G': 1438, 'H': 1535, 'J': 781, 'K': 1367, 'L': 1173, ':': 542, '"': 807,
        'Z': 1295, 'X': 1403, 'C': 1304, 'V': 1394, 'B': 1424, 'N': 1620, 'M': 2019, '<': 1212, '>': 1212, '?': 1150,
        ' ': 514, '\t': 1383, 'avg': 1191,
    },
    'exo 2': {
        '`': 726, '1': 917, '2': 1295, '3': 1281, '4': 1417, '5': 1235, '6': 1318, '7': 1180, '8': 1399, '9': 1318, '0': 1390, '-': 1035, '=': 1348,
        'q': 1314, 'w': 1899, 'e': 1237, 'r': 924, 't': 878, 'y': 1219, 'u': 1314, 'i': 572, 'o': 1309, 'p': 1337, '[': 758, ']': 758, '\\': 1194,
        'a': 1265, 's': 1171, 'd': 1320, 'f': 876, 'g': 1286, 'h': 1339, 'j': 576, 'k': 1173, 'l': 685, ';': 546, "'": 482,
        'z': 1175, 'x': 1217, 'c': 1127, 'v': 1221, 'b': 1314, 'n': 1339, 'm': 2019, ',': 526, '.': 528, '/': 1191,
        '~': 1210, '!': 629, '@': 1655, '#': 1578, '$': 1300, '%': 2060, '^': 982, '&': 1779, '*': 1037, '(': 827, ')': 827, '_': 1332, '+': 1270,
        'Q': 1542, 'W': 2184, 'E': 1290, 'R': 1424, 'T': 1339, 'Y': 1311, 'U': 1535, 'I': 604, 'O': 1539, 'P': 1360, '{': 735, '}': 738, '|': 567,
        'A': 1433, 'S': 1293, 'D': 1530, 'F': 1242, 'G': 1447, 'H': 1539, 'J': 784, 'K': 1362, 'L': 1185, ':': 544, '"': 814,
        'Z': 1304, 'X': 1408, 'C': 1314, 'V': 1403, 'B': 1429, 'N': 1625, 'M': 2021, '<': 1221, '>': 1221, '?': 1164,
        ' ': 514, '\t': 1383, 'avg': 1193,
    },
    'exo 2 thin': {
        '`': 638, '1': 871, '2': 1284, '3': 1272, '4': 1371, '5': 1212, '6': 1300, '7': 1164, '8': 1408, '9': 1302, '0': 1346, '-': 1113, '=': 1357,
        'q': 1288, 'w': 1871, 'e': 1214, 'r': 883, 't': 800, 'y': 1166, 'u': 1290, 'i': 507, 'o': 1290, 'p': 1293, '[': 719, ']': 719, '\\': 1138,
        'a': 1240, 's': 1150, 'd': 1293, 'f': 816, 'g': 1256, 'h': 1311, 'j': 507, 'k': 1090, 'l': 622, ';': 479, "'": 461,
        'z': 1150, 'x': 1164, 'c': 1102, 'v': 1168, 'b': 1286, 'n': 1314, 'm': 2028, ',': 479, '.': 475, '/': 1138,
        '~': 1228, '!': 583, '@': 1597, '#': 1537, '$': 1288, '%': 2035, '^': 903, '&': 1804, '*': 1044, '(': 818, ')': 818, '_': 1357, '+': 1274,
        'Q': 1525, 'W': 2143, 'E': 1270, 'R': 1399, 'T': 1300, 'Y': 1247, 'U': 1537, 'I': 532, 'O': 1525, 'P': 1339, '{': 687, '}': 687, '|': 539,
        'A': 1390, 'S': 1277, 'D': 1519, 'F': 1221, 'G': 1433, 'H': 1514, 'J': 747, 'K': 1295, 'L': 1168, ':': 475, '"': 754,
        'Z': 1286, 'X': 1367, 'C': 1293, 'V': 1364, 'B': 1410, 'N': 1585, 'M': 1968, '<': 1247, '>': 1247, '?': 1168,
        ' ': 530, '\t': 1383, 'avg': 1164,
    },
    'inter bold': {
        '`': 1146, '1': 1128, '2': 1452, '3': 1520, '4': 1562, '5': 1485, '6': 1521, '7': 1371, '8': 1523, '9': 1521, '0': 1586, '-': 1080, '=': 1566,
        'q': 1457, 'w': 1959, 'e': 1377, 'r': 942, 't': 895, 'y': 1350, 'u': 1433, 'i': 627, 'o': 1414, 'p': 1457, '[': 943, ']': 943, '\\': 899,
        'a': 1337, 's': 1295, 'd': 1464, 'f': 889, 'g': 1459, 'h': 1439, 'j': 627, 'k': 1340, 'l': 627, ';': 698, "'": 481,
        'z': 1318, 'x': 1322, 'c': 1354, 'v': 1350, 'b': 1464, 'n': 1433, 'm': 2101, ',': 698, '.': 686, '/': 899,
        '~': 1566, '!': 748, '@': 2375, '#': 1497, '$': 1509, '%': 1980, '^': 1124, '&': 1553, '*': 1301, '(': 943, ')': 943, '_': 1098, '+': 1566,
        'Q': 1804, 'W': 2387, 'E': 1412, 'R': 1512, 'T': 1540, 'Y': 1669, 'U': 1681, 'I': 647, 'O': 1802, 'P': 1493, '{': 943, '}': 943, '|': 861,
        'A': 1723, 'S': 1509, 'D': 1676, 'F': 1348, 'G': 1753, 'H': 1719, 'J': 1313, 'K': 1589, 'L': 1308, ':': 686, '"': 904,
        'Z': 1537, 'X': 1646, 'C': 1733, 'V': 1723, 'B': 1523, 'N': 1694, 'M': 2108, '<': 1566, '>': 1566, '?': 1296,
        ' ': 495, '\t': 2419, 'avg': 1344,
    },
    'inter': {
        '`': 1146, '1': 1070, '2': 1394, '3': 1466, '4': 1480, '5': 1401, '6': 1437, '7': 1316, '8': 1421, '9': 1437, '0': 1440, '-': 1061, '=': 1519,
        'q': 1404, 'w': 1872, 'e': 1342, 'r': 858, 't': 838, 'y': 1283, 'u': 1339, 'i': 547, 'o': 1375, 'p': 1404, '[': 835, ']': 835, '\\': 822,
        'a': 1300, 's': 1205, 'd': 1430, 'f': 832, 'g': 1404, 'h': 1362, 'j': 547, 'k': 1254, 'l': 547, ';': 645, "'": 511,
        'z': 1247, 'x': 1244, 'c': 1286, 'v': 1283, 'b': 1430, 'n': 1349, 'm': 2003, ',': 645, '.': 635, '/': 822,
        '~': 1519, '!': 642, '@': 2157, '#': 1453, '$': 1470, '%': 1872, '^': 1080, '&': 1473, '*': 1152, '(': 835, ')': 835, '_': 1041, '+': 1519,
        'Q': 1754, 'W': 2186, 'E': 1378, 'R': 1473, 'T': 1480, 'Y': 1532, 'U': 1709, 'I': 609, 'O': 1754, 'P': 1463, '{': 835, '}': 835, '|': 753,
        'A': 1558, 'S': 1470, 'D': 1656, 'F': 1352, 'G': 1712, 'H': 1705, 'J': 1250, 'K': 1502, 'L': 1296, ':': 635, '"': 930,
        'Z': 1440, 'X': 1480, 'C': 1676, 'V': 1558, 'B': 1499, 'N': 1735, 'M': 2049, '<': 1519, '>': 1519, '?': 1169,
        ' ': 648, '\t': 2291, 'avg': 1278,
    },
    'inter thin': {
        '`': 1146, '1': 940, '2': 1430, '3': 1414, '4': 1372, '5': 1316, '6': 1342, '7': 1261, '8': 1388, '9': 1342, '0': 1362, '-': 1012, '=': 1466,
        'q': 1339, 'w': 1820, 'e': 1329, 'r': 750, 't': 802, 'y': 1205, 'u': 1309, 'i': 511, 'o': 1336, 'p': 1339, '[': 727, ']': 727, '\\': 745,
        'a': 1247, 's': 1136, 'd': 1378, 'f': 763, 'g': 1345, 'h': 1309, 'j': 511, 'k': 1123, 'l': 498, ';': 596, "'": 472,
        'z': 1172, 'x': 1152, 'c': 1273, 'v': 1205, 'b': 1378, 'n': 1309, 'm': 1925, ',': 592, '.': 583, '/': 745,
        '~': 1466, '!': 602, '@': 2157, '#': 1460, '$': 1411, '%': 1781, '^': 923, '&': 1345, '*': 1152, '(': 727, ')': 727, '_': 984, '+': 1466,
        'Q': 1722, 'W': 2056, 'E': 1345, 'R': 1417, 'T': 1414, 'Y': 1414, 'U': 1728, 'I': 576, 'O': 1722, 'P': 1398, '{': 727, '}': 727, '|': 629,
        'A': 1421, 'S': 1411, 'D': 1646, 'F': 1349, 'G': 1702, 'H': 1696, 'J': 1198, 'K': 1421, 'L': 1296, ':': 583, '"': 786,
        'Z': 1394, 'X': 1349, 'C': 1666, 'V': 1421, 'B': 1476, 'N': 1781, 'M': 1990, '<': 1466, '>': 1466, '?': 1103,
        ' ': 648, '\t': 2164, 'avg': 1218,
    },
    'noto sans bold': {
        '`': 834, '1': 1318, '2': 1318, '3': 1318, '4': 1318, '5': 1318, '6': 1318, '7': 1318, '8': 1318, '9': 1318, '0': 1318, '-': 742, '=': 1318,
        'q': 1459, 'w': 1972, 'e': 1362, 'r': 1046, 't': 1000, 'y': 1311, 'u': 1514, 'i': 703, 'o': 1426, 'p': 1459, '[': 763, ']': 763, '\\': 952,
        'a': 1392, 's': 1145, 'd': 1459, 'f': 892, 'g': 1459, 'h': 1514, 'j': 703, 'k': 1429, 'l': 703, ';': 657, "'": 613,
        'z': 1125, 'x': 1332, 'c': 1185, 'v': 1311, 'b': 1459, 'n': 1514, 'm': 2263, ',': 657, '.': 657, '/': 952,
        '~': 1318, '!': 659, '@': 2067, '#': 1489, '$': 1318, '%': 2076, '^': 1318, '&': 1728, '*': 1256, '(': 781, ')': 781, '_': 947, '+': 1318,
        'Q': 1834, 'W': 2228, 'E': 1290, 'R': 1521, 'T': 1334, 'Y': 1438, 'U': 1742, 'I': 897, 'O': 1834, 'P': 1447, '{': 908, '}': 908, '|': 1270,
        'A': 1590, 'S': 1270, 'D': 1705, 'F': 1265, 'G': 1668, 'H': 1763, 'J': 763, 'K': 1530, 'L': 1302, ':': 657, '"': 1088,
        'Z': 1334, 'X': 1537, 'C': 1468, 'V': 1498, 'B': 1549, 'N': 1873, 'M': 2173, '<': 1318, '>': 1318, '?': 1099,
        ' ': 599, '\t': 1383, 'avg': 1287,
    },
    'noto sans italic': {
        '`': 641, '1': 1270, '2': 1270, '3': 1270, '4': 1270, '5': 1270, '6': 1270, '7': 1270, '8': 1270, '9': 1270, '0': 1270, '-': 721, '=': 1318,
        'q': 1334, 'w': 1666, 'e': 1150, 'r': 917, 't': 765, 'y': 1076, 'u': 1334, 'i': 595, 'o': 1297, 'p': 1334, '[': 668, ']': 668, '\\': 820,
        'a': 1309, 's': 996, 'd': 1334, 'f': 733, 'g': 1334, 'h': 1334, 'j': 595, 'k': 1141, 'l': 595, ';': 590, "'": 507,
        'z': 1026, 'x': 1113, 'c': 1044, 'v': 1076, 'b': 1334, 'n': 1334, 'm': 2016, ',': 590, '.': 590, '/': 820,
        '~': 1318, '!': 602, '@': 1954, '#': 1489, '$': 1270, '%': 1841, '^': 1318, '&': 1551, '*': 1270, '(': 668, ')': 668, '_': 910, '+': 1318,
        'Q': 1661, 'W': 1972, 'E': 1185, 'R': 1320, 'T': 1155, 'Y': 1173, 'U': 1560, 'I': 747, 'O': 1661, 'P': 1307, '{': 807, '}': 807, '|': 1270,
        'A': 1295, 'S': 1164, 'D': 1537, 'F': 1099, 'G': 1562, 'H': 1567, 'J': 629, 'K': 1295, 'L': 1102, ':': 590, '"': 903,
        'Z': 1224, 'X': 1214, 'C': 1353, 'V': 1272, 'B': 1383, 'N': 1629, 'M': 1938, '<': 1318, '>': 1318, '?': 991,
        ' ': 599, '\t': 1383, 'avg': 1159,
    },
    'noto sans': {
        '`': 648, '1': 1318, '2': 1318, '3': 1318, '4': 1318, '5': 1318, '6': 1318, '7': 1318, '8': 1318, '9': 1318, '0': 1318, '-': 742, '=': 1318,
        'q': 1417, 'w': 1811, 'e': 1300, 'r': 952, 't': 832, 'y': 1175, 'u': 1424, 'i': 595, 'o': 1394, 'p': 1417, '[': 758, ']': 758, '\\': 857,
        'a': 1293, 's': 1104, 'd': 1417, 'f': 793, 'g': 1417, 'h': 1424, 'j': 595, 'k': 1231, 'l': 595, ';': 618, "'": 519,
        'z': 1083, 'x': 1219, 'c': 1106, 'v': 1171, 'b': 1417, 'n': 1424, 'm': 2154, ',': 618, '.': 618, '/': 857,
        '~': 1318, '!': 620, '@': 2072, '#': 1489, '$': 1318, '%': 1915, '^': 1318, '&': 1687, '*': 1270, '(': 691, ')': 691, '_': 1023, '+': 1318,
        'Q': 1800, 'W': 2143, 'E': 1281, 'R': 1433, 'T': 1281, 'Y': 1304, 'U': 1684, 'I': 781, 'O': 1800, 'P': 1394, '{': 876, '}': 876, '|': 1270,
        'A': 1473, 'S': 1265, 'D': 1682, 'F': 1196, 'G': 1678, 'H': 1708, 'J': 629, 'K': 1426, 'L': 1208, ':': 618, '"': 940,
        'Z': 1318, 'X': 1350, 'C': 1456, 'V': 1383, 'B': 1498, 'N': 1751, 'M': 2090, '<': 1318, '>': 1318, '?': 1000,
        ' ': 599, '\t': 1383, 'avg': 1229,
    },
    'noto sans thin': {
        '`': 574, '1': 1314, '2': 1314, '3': 1314, '4': 1314, '5': 1314, '6': 1314, '7': 1314, '8': 1314, '9': 1314, '0': 1314, '-': 742, '=': 1314,
        'q': 1343, 'w': 1578, 'e': 1244, 'r': 864, 't': 735, 'y': 986, 'u': 1320, 'i': 477, 'o': 1325, 'p': 1343, '[': 721, ']': 721, '\\': 742,
        'a': 1178, 's': 1055, 'd': 1343, 'f': 632, 'g': 1343, 'h': 1320, 'j': 477, 'k': 1046, 'l': 477, ';': 507, "'": 397,
        'z': 1051, 'x': 1104, 'c': 1088, 'v': 991, 'b': 1343, 'n': 1320, 'm': 1963, ',': 422, '.': 507, '/': 742,
        '~': 1314, '!': 514, '@': 2023, '#': 1489, '$': 1314, '%': 1827, '^': 1314, '&': 1618, '*': 1270, '(': 588, ')': 588, '_': 901, '+': 1314,
        'Q': 1737, 'W': 2002, 'E': 1267, 'R': 1330, 'T': 1164, 'Y': 1164, 'U': 1641, 'I': 652, 'O': 1737, 'P': 1320, '{': 777, '}': 777, '|': 1233,
        'A': 1330, 'S': 1251, 'D': 1595, 'F': 1134, 'G': 1659, 'H': 1627, 'J': 532, 'K': 1286, 'L': 1166, ':': 507, '"': 738,
        'Z': 1332, 'X': 1173, 'C': 1415, 'V': 1297, 'B': 1413, 'N': 1608, 'M': 1929, '<': 1314, '>': 1314, '?': 952,
        ' ': 599, '\t': 1383, 'avg': 1152,
    },
    'tahoma': {
        '`': 1258, '1': 1258, '2': 1258, '3': 1258, '4': 1258, '5': 1258, '6': 1258, '7': 1258, '8': 1258, '9': 1258, '0': 1258, '-': 837, '=': 1677,
        'q': 1274, 'w': 1710, 'e': 1213, 'r': 831, 't': 771, 'y': 1148, 'u': 1285, 'i': 527, 'o': 1251, 'p': 1274, '[': 882, ']': 882, '\\': 881,
        'a': 1210, 's': 1029, 'd': 1274, 'f': 734, 'g': 1274, 'h': 1285, 'j': 649, 'k': 1148, 'l': 527, ';': 815, "'": 486,
        'z': 1024, 'x': 1141, 'c': 1063, 'v': 1148, 'b': 1274, 'n': 1285, 'm': 1935, ',': 698, '.': 698, '/': 881,
        '~': 1677, '!': 765, '@': 2095, '#': 1677, '$': 1258, '%': 2250, '^': 1677, '&': 1553, '*': 1258, '(': 882, ')': 882, '_': 1258, '+': 1677,
        'Q': 1630, 'W': 2078, 'E': 1293, 'R': 1430, 'T': 1346, 'Y': 1328, 'U': 1511, 'I': 860, 'O': 1630, 'P': 1270, '{': 1107, '}': 1107, '|': 881,
        'A': 1382, 'S': 1284, 'D': 1563, 'F': 1202, 'G': 1538, 'H': 1556, 'J': 960, 'K': 1355, 'L': 1147, ':': 815, '"': 925,
        'Z': 1288, 'X': 1338, 'C': 1384, 'V': 1375, 'B': 1358, 'N': 1538, 'M': 1776, '<': 1677, '>': 1677, '?': 1092,
        ' ': 720, '\t': 2304, 'avg': 1233,
    },
    'tahoma bold': {
        '`': 1258, '1': 1467, '2': 1467, '3': 1467, '4': 1467, '5': 1467, '6': 1467, '7': 1467, '8': 1467, '9': 1467, '0': 1467, '-': 994, '=': 1886,
        'q': 1450, 'w': 2050, 'e': 1368, 'r': 999, 't': 958, 'y': 1327, 'u': 1475, 'i': 696, 'o': 1422, 'p': 1450, '[': 1047, ']': 1047, '\\': 1330,
        'a': 1380, 's': 1186, 'd': 1450, 'f': 881, 'g': 1450, 'h': 1475, 'j': 836, 'k': 1389, 'l': 696, ';': 837, "'": 635,
        'z': 1212, 'x': 1393, 'c': 1215, 'v': 1333, 'b': 1456, 'n': 1475, 'm': 2197, ',': 720, '.': 720, '/': 1330,
        '~': 1886, '!': 790, '@': 2120, '#': 1886, '$': 1467, '%': 2762, '^': 1886, '&': 1800, '*': 1467, '(': 1047, ')': 1047, '_': 1467, '+': 1886,
        'Q': 1774, 'W': 2368, 'E': 1418, 'R': 1673, 'T': 1411, 'Y': 1545, 'U': 1702, 'I': 1114, 'O': 1774, 'P': 1515, '{': 1436, '}': 1436, '|': 1467,
        'A': 1578, 'S': 1459, 'D': 1745, 'F': 1339, 'G': 1717, 'H': 1761, 'J': 1153, 'K': 1605, 'L': 1319, ':': 837, '"': 1128,
        'Z': 1435, 'X': 1578, 'C': 1538, 'V': 1555, 'B': 1581, 'N': 1776, 'M': 2058, '<': 1886, '>': 1886, '?': 1305,
        ' ': 675, '\t': 2304, 'avg': 1428,
    },
    'times new roman': {
        '`': 768, '1': 1152, '2': 1152, '3': 1152, '4': 1152, '5': 1152, '6': 1152, '7': 1152, '8': 1152, '9': 1152, '0': 1152, '-': 768, '=': 1300,
        'q': 1152, 'w': 1664, 'e': 1023, 'r': 768, 't': 640, 'y': 1152, 'u': 1152, 'i': 640, 'o': 1152, 'p': 1152, '[': 768, ']': 768, '\\': 640,
        'a': 1023, 's': 897, 'd': 1152, 'f': 768, 'g': 1152, 'h': 1152, 'j': 640, 'k': 1152, 'l': 640, ';': 640, "'": 415,
        'z': 1023, 'x': 1152, 'c': 1023, 'v': 1152, 'b': 1152, 'n': 1152, 'm': 1792, ',': 576, '.': 576, '/': 640,
        '~': 1247, '!': 768, '@': 2122, '#': 1152, '$': 1152, '%': 1920, '^': 1081, '&': 1792, '*': 1152, '(': 768, ')': 768, '_': 1152, '+': 1300,
        'Q': 1664, 'W': 2175, 'E': 1408, 'R': 1537, 'T': 1408, 'Y': 1664, 'U': 1664, 'I': 768, 'O': 1664, 'P': 1282, '{': 1106, '}': 1106, '|': 462,
        'A': 1664, 'S': 1282, 'D': 1664, 'F': 1282, 'G': 1664, 'H': 1664, 'J': 897, 'K': 1664, 'L': 1408, ':': 640, '"': 941,
        'Z': 1408, 'X': 1664, 'C': 1537, 'V': 1664, 'B': 1537, 'N': 1664, 'M': 2049, '<': 1300, '>': 1300, '?': 1023,
        ' ': 576, '\t': 1792, 'avg': 1175,
    },
    'times new roman bold': {
        '`': 768, '1': 1152, '2': 1152, '3': 1152, '4': 1152, '5': 1152, '6': 1152, '7': 1152, '8': 1152, '9': 1152, '0': 1152, '-': 768, '=': 1313,
        'q': 1282, 'w': 1664, 'e': 1023, 'r': 1023, 't': 768, 'y': 1152, 'u': 1282, 'i': 640, 'o': 1152, 'p': 1282, '[': 768, ']': 768, '\\': 640,
        'a': 1152, 's': 897, 'd': 1282, 'f': 768, 'g': 1152, 'h': 1282, 'j': 768, 'k': 1282, 'l': 640, ';': 768, "'": 640,
        'z': 1023, 'x': 1152, 'c': 1023, 'v': 1152, 'b': 1282, 'n': 1282, 'm': 1920, ',': 576, '.': 576, '/': 640,
        '~': 1198, '!': 768, '@': 2143, '#': 1152, '$': 1152, '%': 2304, '^': 1339, '&': 1920, '*': 1152, '(': 768, ')': 768, '_': 1152, '+': 1313,
        'Q': 1792, 'W': 2304, 'E': 1537, 'R': 1664, 'T': 1537, 'Y': 1664, 'U': 1664, 'I': 897, 'O': 1792, 'P': 1408, '{': 908, '}': 908, '|': 508,
        'A': 1664, 'S': 1282, 'D': 1664, 'F': 1408, 'G': 1792, 'H': 1792, 'J': 1152, 'K': 1792, 'L': 1537, ':': 768, '"': 1279,
        'Z': 1537, 'X': 1664, 'C': 1664, 'V': 1664, 'B': 1537, 'N': 1664, 'M': 2175, '<': 1313, '>': 1313, '?': 1152,
        ' ': 576, '\t': 1792, 'avg': 1233,
    },
    'times new roman italic': {
        '`': 768, '1': 1152, '2': 1152, '3': 1152, '4': 1152, '5': 1152, '6': 1152, '7': 1152, '8': 1152, '9': 1152, '0': 1152, '-': 768, '=': 1555,
        'q': 1152, 'w': 1537, 'e': 1023, 'r': 897, 't': 640, 'y': 1023, 'u': 1152, 'i': 640, 'o': 1152, 'p': 1152, '[': 897, ']': 897, '\\': 640,
        'a': 1152, 's': 897, 'd': 1152, 'f': 640, 'g': 1152, 'h': 1152, 'j': 640, 'k': 1023, 'l': 640, ';': 768, "'": 493,
        'z': 897, 'x': 1023, 'c': 1023, 'v': 1023, 'b': 1152, 'n': 1152, 'm': 1664, ',': 576, '.': 576, '/': 640,
        '~': 1247, '!': 768, '@': 2120, '#': 1152, '$': 1152, '%': 1920, '^': 972, '&': 1792, '*': 1152, '(': 768, ')': 768, '_': 1152, '+': 1555,
        'Q': 1664, 'W': 1920, 'E': 1408, 'R': 1408, 'T': 1282, 'Y': 1282, 'U': 1664, 'I': 768, 'O': 1664, 'P': 1408, '{': 922, '}': 922, '|': 634,
        'A': 1408, 'S': 1152, 'D': 1664, 'F': 1408, 'G': 1664, 'H': 1664, 'J': 1023, 'K': 1537, 'L': 1282, ':': 768, '"': 968,
        'Z': 1282, 'X': 1408, 'C': 1537, 'V': 1408, 'B': 1408, 'N': 1537, 'M': 1920, '<': 1555, '>': 1555, '?': 1152,
        ' ': 576, '\t': 1792, 'avg': 1159,
    },
    'torus bold': {
        '`': 680, '1': 1325, '2': 1325, '3': 1325, '4': 1325, '5': 1325, '6': 1325, '7': 1325, '8': 1325, '9': 1325, '0': 1325, '-': 885, '=': 917,
        'q': 1196, 'w': 1555, 'e': 1148, 'r': 1097, 't': 940, 'y': 1166, 'u': 1300, 'i': 655, 'o': 1224, 'p': 1235, '[': 652, ']': 652, '\\': 1127,
        'a': 1104, 's': 1016, 'd': 1182, 'f': 873, 'g': 1191, 'h': 1214, 'j': 655, 'k': 1134, 'l': 625, ';': 512, "'": 558,
        'z': 1145, 'x': 1076, 'c': 1072, 'v': 1166, 'b': 1203, 'n': 1247, 'm': 1853, ',': 567, '.': 523, '/': 1127,
        '~': 1221, '!': 661, '@': 1422, '#': 1208, '$': 1244, '%': 2134, '^': 1214, '&': 1500, '*': 1371, '(': 705, ')': 705, '_': 1173, '+': 1090,
        'Q': 1567, 'W': 1924, 'E': 1115, 'R': 1244, 'T': 1088, 'Y': 1182, 'U': 1493, 'I': 643, 'O': 1514, 'P': 1194, '{': 708, '}': 708, '|': 632,
        'A': 1493, 'S': 1205, 'D': 1401, 'F': 1055, 'G': 1496, 'H': 1376, 'J': 655, 'K': 1343, 'L': 1104, ':': 523, '"': 1005,
        'Z': 1258, 'X': 1228, 'C': 1323, 'V': 1426, 'B': 1235, 'N': 1376, 'M': 1634, '<': 797, '>': 797, '?': 963,
        ' ': 590, '\t': 1152, 'avg': 1122,
    },
    'torus italic': {
        '`': 680, '1': 1247, '2': 1247, '3': 1247, '4': 1247, '5': 1247, '6': 1247, '7': 1247, '8': 1247, '9': 1247, '0': 1247, '-': 820, '=': 825,
        'q': 1102, 'w': 1477, 'e': 1055, 'r': 1007, 't': 841, 'y': 1055, 'u': 1214, 'i': 542, 'o': 1134, 'p': 1148, '[': 560, ']': 565, '\\': 1053,
        'a': 1081, 's': 913, 'd': 1088, 'f': 777, 'g': 1097, 'h': 1125, 'j': 539, 'k': 1049, 'l': 512, ';': 440, "'": 484,
        'z': 1042, 'x': 954, 'c': 963, 'v': 1072, 'b': 1118, 'n': 1157, 'm': 1784, ',': 493, '.': 429, '/': 1019,
        '~': 1171, '!': 562, '@': 1341, '#': 1104, '$': 1150, '%': 1894, '^': 1196, '&': 1410, '*': 1279, '(': 585, ')': 585, '_': 1102, '+': 1012,
        'Q': 1482, 'W': 1839, 'E': 1023, 'R': 1145, 'T': 1007, 'Y': 1079, 'U': 1410, 'I': 523, 'O': 1440, 'P': 1104, '{': 606, '}': 606, '|': 509,
        'A': 1401, 'S': 1106, 'D': 1318, 'F': 961, 'G': 1417, 'H': 1286, 'J': 532, 'K': 1258, 'L': 1014, ':': 429, '"': 862,
        'Z': 1161, 'X': 1161, 'C': 1277, 'V': 1327, 'B': 1145, 'N': 1286, 'M': 1551, '<': 678, '>': 675, '?': 883,
        ' ': 584, '\t': 1123, 'avg': 1033,
    },
    'torus': {
        '`': 708, '1': 1300, '2': 1300, '3': 1300, '4': 1300, '5': 1300, '6': 1300, '7': 1300, '8': 1300, '9': 1300, '0': 1300, '-': 855, '=': 860,
        'q': 1148, 'w': 1537, 'e': 1099, 'r': 1049, 't': 878, 'y': 1099, 'u': 1265, 'i': 565, 'o': 1182, 'p': 1196, '[': 583, ']': 588, '\\': 1097,
        'a': 1028, 's': 952, 'd': 1134, 'f': 809, 'g': 1143, 'h': 1173, 'j': 562, 'k': 1092, 'l': 535, ';': 459, "'": 505,
        'z': 1085, 'x': 993, 'c': 1002, 'v': 1118, 'b': 1164, 'n': 1203, 'm': 1857, ',': 514, '.': 447, '/': 1060,
        '~': 1219, '!': 585, '@': 1396, '#': 1150, '$': 1198, '%': 1975, '^': 1247, '&': 1468, '*': 1332, '(': 611, ')': 611, '_': 1148, '+': 1053,
        'Q': 1544, 'W': 1915, 'E': 1067, 'R': 1191, 'T': 1049, 'Y': 1125, 'U': 1468, 'I': 546, 'O': 1500, 'P': 1150, '{': 632, '}': 632, '|': 530,
        'A': 1459, 'S': 1152, 'D': 1371, 'F': 1002, 'G': 1475, 'H': 1339, 'J': 553, 'K': 1311, 'L': 1055, ':': 447, '"': 897,
        'Z': 1210, 'X': 1210, 'C': 1277, 'V': 1383, 'B': 1191, 'N': 1339, 'M': 1615, '<': 705, '>': 703, '?': 920,
        ' ': 590, '\t': 1152, 'avg': 1074,
    },
    'ubuntu bold': {
        '`': 659, '1': 1309, '2': 1309, '3': 1309, '4': 1309, '5': 1309, '6': 1309, '7': 1309, '8': 1309, '9': 1309, '0': 1309, '-': 784, '=': 1309,
        'q': 1392, 'w': 1807, 'e': 1346, 'r': 973, 't': 1023, 'y': 1261, 'u': 1357, 'i': 666, 'o': 1399, 'p': 1392, '[': 855, ']': 855, '\\': 1007,
        'a': 1274, 's': 1118, 'd': 1392, 'f': 973, 'g': 1369, 'h': 1357, 'j': 666, 'k': 1334, 'l': 728, ';': 567, "'": 569,
        'z': 1152, 'x': 1277, 'c': 1152, 'v': 1267, 'b': 1392, 'n': 1357, 'm': 1986, ',': 567, '.': 567, '/': 1007,
        '~': 1309, '!': 659, '@': 2244, '#': 1611, '$': 1309, '%': 2115, '^': 1309, '&': 1625, '*': 1157, '(': 820, ')': 820, '_': 1152, '+': 1309,
        'Q': 1820, 'W': 2184, 'E': 1396, 'R': 1537, 'T': 1415, 'Y': 1523, 'U': 1629, 'I': 728, 'O': 1820, 'P': 1484, '{': 855, '}': 855, '|': 742,
        'A': 1661, 'S': 1341, 'D': 1698, 'F': 1323, 'G': 1618, 'H': 1691, 'J': 1219, 'K': 1576, 'L': 1297, ':': 567, '"': 1072,
        'Z': 1406, 'X': 1555, 'C': 1493, 'V': 1664, 'B': 1549, 'N': 1742, 'M': 2067, '<': 1309, '>': 1309, '?': 1049,
        ' ': 588, '\t': 588, 'avg': 1268,
    },
    'ubuntu italic': {
        '`': 867, '1': 1300, '2': 1300, '3': 1300, '4': 1300, '5': 1300, '6': 1300, '7': 1300, '8': 1300, '9': 1300, '0': 1300, '-': 675, '=': 1300,
        'q': 1270, 'w': 1813, 'e': 1194, 'r': 857, 't': 897, 'y': 1102, 'u': 1267, 'i': 574, 'o': 1279, 'p': 1258, '[': 742, ']': 742, '\\': 867,
        'a': 1254, 's': 949, 'd': 1261, 'f': 848, 'g': 1267, 'h': 1272, 'j': 574, 'k': 1191, 'l': 636, ';': 567, "'": 544,
        'z': 1046, 'x': 1090, 'c': 1039, 'v': 1108, 'b': 1279, 'n': 1270, 'm': 1908, ',': 567, '.': 567, '/': 867,
        '~': 1300, '!': 576, '@': 2145, '#': 1507, '$': 1300, '%': 1926, '^': 1300, '&': 1475, '*': 1083, '(': 731, ')': 731, '_': 1143, '+': 1300,
        'Q': 1703, 'W': 2111, 'E': 1284, 'R': 1403, 'T': 1279, 'Y': 1318, 'U': 1535, 'I': 606, 'O': 1703, 'P': 1360, '{': 751, '}': 751, '|': 634,
        'A': 1466, 'S': 1191, 'D': 1592, 'F': 1208, 'G': 1500, 'H': 1569, 'J': 1111, 'K': 1415, 'L': 1168, ':': 567, '"': 945,
        'Z': 1293, 'X': 1394, 'C': 1383, 'V': 1436, 'B': 1424, 'N': 1608, 'M': 1952, '<': 1300, '>': 1300, '?': 885,
        ' ': 564, '\t': 564, 'avg': 1179,
    },
    'ubuntu light': {
        '`': 867, '1': 1300, '2': 1300, '3': 1300, '4': 1300, '5': 1300, '6': 1300, '7': 1300, '8': 1300, '9': 1300, '0': 1300, '-': 650, '=': 1300,
        'q': 1343, 'w': 1786, 'e': 1263, 'r': 862, 't': 892, 'y': 1092, 'u': 1311, 'i': 546, 'o': 1341, 'p': 1343, '[': 714, ']': 714, '\\': 827,
        'a': 1173, 's': 986, 'd': 1343, 'f': 867, 'g': 1320, 'h': 1311, 'j': 546, 'k': 1141, 'l': 588, ';': 567, "'": 553,
        'z': 1049, 'x': 1150, 'c': 1060, 'v': 1106, 'b': 1343, 'n': 1311, 'm': 1989, ',': 567, '.': 567, '/': 827,
        '~': 1300, '!': 632, '@': 2166, '#': 1505, '$': 1300, '%': 1915, '^': 1300, '&': 1493, '*': 1088, '(': 714, ')': 714, '_': 1129, '+': 1300,
        'Q': 1784, 'W': 2122, 'E': 1279, 'R': 1424, 'T': 1263, 'Y': 1311, 'U': 1567, 'I': 572, 'O': 1784, 'P': 1364, '{': 735, '}': 735, '|': 604,
        'A': 1477, 'S': 1191, 'D': 1618, 'F': 1198, 'G': 1516, 'H': 1595, 'J': 1122, 'K': 1380, 'L': 1157, ':': 567, '"': 903,
        'Z': 1290, 'X': 1401, 'C': 1403, 'V': 1438, 'B': 1452, 'N': 1634, 'M': 1993, '<': 1300, '>': 1300, '?': 876,
        ' ': 526, '\t': 526, 'avg': 1185,
    },
    'ubuntu': {
        '`': 867, '1': 1300, '2': 1300, '3': 1300, '4': 1300, '5': 1300, '6': 1300, '7': 1300, '8': 1300, '9': 1300, '0': 1300, '-': 689, '=': 1300,
        'q': 1357, 'w': 1790, 'e': 1288, 'r': 890, 't': 926, 'y': 1145, 'u': 1323, 'i': 583, 'o': 1360, 'p': 1357, '[': 758, ']': 758, '\\': 885,
        'a': 1203, 's': 1028, 'd': 1357, 'f': 890, 'g': 1332, 'h': 1316, 'j': 583, 'k': 1203, 'l': 629, ';': 567, "'": 556,
        'z': 1085, 'x': 1178, 'c': 1072, 'v': 1157, 'b': 1357, 'n': 1323, 'm': 1984, ',': 567, '.': 567, '/': 885,
        '~': 1300, '!': 636, '@': 2189, '#': 1537, '$': 1300, '%': 1977, '^': 1300, '&': 1535, '*': 1106, '(': 747, ')': 747, '_': 1134, '+': 1300,
        'Q': 1793, 'W': 2141, 'E': 1316, 'R': 1449, 'T': 1302, 'Y': 1378, 'U': 1585, 'I': 620, 'O': 1793, 'P': 1401, '{': 767, '}': 767, '|': 643,
        'A': 1528, 'S': 1226, 'D': 1643, 'F': 1237, 'G': 1549, 'H': 1625, 'J': 1152, 'K': 1449, 'L': 1196, ':': 567, '"': 963,
        'Z': 1320, 'X': 1454, 'C': 1429, 'V': 1512, 'B': 1482, 'N': 1678, 'M': 2007, '<': 1300, '>': 1300, '?': 931,
        ' ': 532, '\t': 532, 'avg': 1210,
    },
    'venera 100': {
        '`': 1152, '1': 1009, '2': 2076, '3': 2113, '4': 2219, '5': 2129, '6': 2194, '7': 1943, '8': 2150, '9': 2217, '0': 2355, '-': 1323, '=': 1855,
        'q': 2447, 'w': 3244, 'e': 2258, 'r': 2265, 't': 1880, 'y': 1903, 'u': 2330, 'i': 544, 'o': 2447, 'p': 2175, '[': 1009, ']': 1009, '\\': 1155,
        'a': 2231, 's': 2131, 'd': 2415, 'f': 2198, 'g': 2313, 'h': 2318, 'j': 1756, 'k': 2085, 'l': 1984, ';': 629, "'": 470,
        'z': 2053, 'x': 1820, 'c': 2203, 'v': 2210, 'b': 2341, 'n': 2484, 'm': 2668, ',': 539, '.': 477, '/': 1155,
        '~': 1894, '!': 551, '@': 2408, '#': 1894, '$': 2152, '%': 2311, '^': 1500, '&': 2182, '*': 1265, '(': 590, ')': 590, '_': 2055, '+': 1698,
        'Q': 2447, 'W': 3244, 'E': 2258, 'R': 2265, 'T': 1880, 'Y': 1903, 'U': 2330, 'I': 544, 'O': 2447, 'P': 2175, '{': 899, '}': 899, '|': 526,
        'A': 2231, 'S': 2131, 'D': 2415, 'F': 2198, 'G': 2313, 'H': 2318, 'J': 1756, 'K': 2085, 'L': 1984, ':': 475, '"': 906,
        'Z': 2053, 'X': 1820, 'C': 2203, 'V': 2210, 'B': 2341, 'N': 2484, 'M': 2668, '<': 1360, '>': 1360, '?': 1578,
        ' ': 784, '\t': 687, 'avg': 1836,
    },
    'venera 300': {
        '`': 1152, '1': 1007, '2': 2090, '3': 2106, '4': 2235, '5': 2131, '6': 2210, '7': 1945, '8': 2161, '9': 2228, '0': 2357, '-': 1311, '=': 1834,
        'q': 2447, 'w': 3272, 'e': 2265, 'r': 2279, 't': 1906, 'y': 1998, 'u': 2323, 'i': 585, 'o': 2447, 'p': 2194, '[': 989, ']': 989, '\\': 1191,
        'a': 2265, 's': 2157, 'd': 2403, 'f': 2194, 'g': 2309, 'h': 2332, 'j': 1758, 'k': 2106, 'l': 2000, ';': 678, "'": 516,
        'z': 2072, 'x': 1901, 'c': 2221, 'v': 2240, 'b': 2332, 'n': 2479, 'm': 2698, ',': 597, '.': 535, '/': 1191,
        '~': 1871, '!': 590, '@': 2417, '#': 1864, '$': 2159, '%': 2355, '^': 1507, '&': 2203, '*': 1247, '(': 629, ')': 629, '_': 2035, '+': 1673,
        'Q': 2447, 'W': 3272, 'E': 2265, 'R': 2279, 'T': 1906, 'Y': 1998, 'U': 2323, 'I': 585, 'O': 2447, 'P': 2194, '{': 943, '}': 945, '|': 558,
        'A': 2265, 'S': 2157, 'D': 2403, 'F': 2194, 'G': 2309, 'H': 2332, 'J': 1758, 'K': 2106, 'L': 2000, ':': 532, '"': 977,
        'Z': 2072, 'X': 1901, 'C': 2221, 'V': 2240, 'B': 2332, 'N': 2479, 'M': 2698, '<': 1339, '>': 1339, '?': 1581,
        ' ': 763, '\t': 687, 'avg': 1852,
    },
    'venera 500': {
        '`': 1152, '1': 1007, '2': 2099, '3': 2104, '4': 2256, '5': 2134, '6': 2224, '7': 1959, '8': 2168, '9': 2237, '0': 2360, '-': 1277, '=': 1813,
        'q': 2449, 'w': 3290, 'e': 2274, 'r': 2293, 't': 1938, 'y': 2081, 'u': 2313, 'i': 632, 'o': 2449, 'p': 2217, '[': 966, ']': 966, '\\': 1224,
        'a': 2297, 's': 2180, 'd': 2392, 'f': 2189, 'g': 2307, 'h': 2343, 'j': 1758, 'k': 2129, 'l': 2019, ';': 724, "'": 567,
        'z': 2069, 'x': 1977, 'c': 2233, 'v': 2272, 'b': 2327, 'n': 2472, 'm': 2726, ',': 641, '.': 592, '/': 1221,
        '~': 1843, '!': 632, '@': 2426, '#': 1846, '$': 2168, '%': 2401, '^': 1514, '&': 2226, '*': 1240, '(': 671, ')': 671, '_': 2019, '+': 1657,
        'Q': 2449, 'W': 3290, 'E': 2274, 'R': 2293, 'T': 1938, 'Y': 2081, 'U': 2313, 'I': 632, 'O': 2449, 'P': 2217, '{': 989, '}': 989, '|': 590,
        'A': 2297, 'S': 2180, 'D': 2392, 'F': 2189, 'G': 2307, 'H': 2343, 'J': 1758, 'K': 2129, 'L': 2019, ':': 590, '"': 1051,
        'Z': 2069, 'X': 1977, 'C': 2233, 'V': 2272, 'B': 2327, 'N': 2472, 'M': 2726, '<': 1320, '>': 1320, '?': 1590,
        ' ': 738, '\t': 687, 'avg': 1867,
    },
    'venera 700': {
        '`': 1152, '1': 1009, '2': 2113, '3': 2095, '4': 2274, '5': 2134, '6': 2240, '7': 1966, '8': 2175, '9': 2247, '0': 2362, '-': 1242, '=': 1793,
        'q': 2449, 'w': 3318, 'e': 2279, 'r': 2309, 't': 1963, 'y': 2171, 'u': 2307, 'i': 678, 'o': 2452, 'p': 2240, '[': 947, ']': 947, '\\': 1267,
        'a': 2325, 's': 2201, 'd': 2385, 'f': 2182, 'g': 2295, 'h': 2362, 'j': 1756, 'k': 2154, 'l': 2030, ';': 774, "'": 613,
        'z': 2099, 'x': 2060, 'c': 2242, 'v': 2300, 'b': 2323, 'n': 2475, 'm': 2758, ',': 701, '.': 648, '/': 1267,
        '~': 1820, '!': 675, '@': 2436, '#': 1827, '$': 2182, '%': 2447, '^': 1523, '&': 2247, '*': 1235, '(': 710, ')': 710, '_': 2000, '+': 1641,
        'Q': 2449, 'W': 3318, 'E': 2279, 'R': 2309, 'T': 1963, 'Y': 2171, 'U': 2307, 'I': 678, 'O': 2452, 'P': 2240, '{': 1039, '}': 1039, '|': 627,
        'A': 2325, 'S': 2201, 'D': 2385, 'F': 2182, 'G': 2295, 'H': 2362, 'J': 1756, 'K': 2154, 'L': 2030, ':': 648, '"': 1125,
        'Z': 2099, 'X': 2060, 'C': 2242, 'V': 2300, 'B': 2323, 'N': 2475, 'M': 2758, '<': 1309, '>': 1309, '?': 1602,
        ' ': 717, '\t': 687, 'avg': 1884,
    },
    'venera 900': {
        '`': 1152, '1': 1012, '2': 2122, '3': 2088, '4': 2295, '5': 2136, '6': 2256, '7': 1982, '8': 2184, '9': 2256, '0': 2364, '-': 1208, '=': 1772,
        'q': 2447, 'w': 3339, 'e': 2284, 'r': 2323, 't': 1984, 'y': 2260, 'u': 2297, 'i': 721, 'o': 2447, 'p': 2263, '[': 926, ']': 926, '\\': 1314,
        'a': 2364, 's': 2221, 'd': 2373, 'f': 2173, 'g': 2288, 'h': 2373, 'j': 1754, 'k': 2175, 'l': 2042, ';': 823, "'": 661,
        'z': 2108, 'x': 2138, 'c': 2249, 'v': 2332, 'b': 2313, 'n': 2468, 'm': 2788, ',': 744, '.': 705, '/': 1314,
        '~': 1800, '!': 714, '@': 2442, '#': 1811, '$': 2203, '%': 2512, '^': 1530, '&': 2270, '*': 1231, '(': 749, ')': 749, '_': 1979, '+': 1629,
        'Q': 2447, 'W': 3339, 'E': 2284, 'R': 2323, 'T': 1984, 'Y': 2260, 'U': 2297, 'I': 721, 'O': 2447, 'P': 2263, '{': 1088, '}': 1088, '|': 659,
        'A': 2364, 'S': 2221, 'D': 2373, 'F': 2173, 'G': 2288, 'H': 2373, 'J': 1754, 'K': 2175, 'L': 2042, ':': 705, '"': 1196,
        'Z': 2108, 'X': 2138, 'C': 2249, 'V': 2332, 'B': 2313, 'N': 2468, 'M': 2788, '<': 1309, '>': 1309, '?': 1618,
        ' ': 691, '\t': 687, 'avg': 1900,
    },
    'verdana': {
        '`': 1465, '1': 1465, '2': 1465, '3': 1465, '4': 1465, '5': 1465, '6': 1465, '7': 1465, '8': 1465, '9': 1465, '0': 1465, '-': 1047, '=': 1886,
        'q': 1436, 'w': 1886, 'e': 1373, 'r': 984, 't': 908, 'y': 1364, 'u': 1458, 'i': 633, 'o': 1399, 'p': 1436, '[': 1047, ']': 1047, '\\': 1047,
        'a': 1384, 's': 1201, 'd': 1436, 'f': 810, 'g': 1436, 'h': 1458, 'j': 793, 'k': 1364, 'l': 633, ';': 1047, "'": 619,
        'z': 1211, 'x': 1364, 'c': 1201, 'v': 1364, 'b': 1436, 'n': 1458, 'm': 2241, ',': 838, '.': 838, '/': 1047,
        '~': 1886, '!': 907, '@': 2304, '#': 1886, '$': 1465, '%': 2480, '^': 1886, '&': 1674, '*': 1465, '(': 1047, ')': 1047, '_': 1465, '+': 1886,
        'Q': 1814, 'W': 2278, 'E': 1457, 'R': 1602, 'T': 1420, 'Y': 1418, 'U': 1687, 'I': 970, 'O': 1814, 'P': 1390, '{': 1463, '}': 1463, '|': 1047,
        'A': 1575, 'S': 1575, 'D': 1776, 'F': 1324, 'G': 1787, 'H': 1732, 'J': 1048, 'K': 1597, 'L': 1283, ':': 1047, '"': 1058,
        'Z': 1579, 'X': 1579, 'C': 1609, 'V': 1575, 'B': 1580, 'N': 1724, 'M': 1942, '<': 1886, '>': 1886, '?': 1257,
        ' ': 810, '\t': 2304, 'avg': 1417,
    },
    'verdana bold': {
        '`': 1638, '1': 1638, '2': 1638, '3': 1638, '4': 1638, '5': 1638, '6': 1638, '7': 1638, '8': 1638, '9': 1638, '0': 1638, '-': 1106, '=': 1998,
        'q': 1611, 'w': 2257, 'e': 1530, 'r': 1146, 't': 1050, 'y': 1500, 'u': 1642, 'i': 788, 'o': 1582, 'p': 1611, '[': 1252, ']': 1252, '\\': 1589,
        'a': 1539, 's': 1367, 'd': 1611, 'f': 973, 'g': 1611, 'h': 1642, 'j': 928, 'k': 1546, 'l': 788, ';': 927, "'": 765,
        'z': 1375, 'x': 1542, 'c': 1356, 'v': 1498, 'b': 1611, 'n': 1642, 'm': 2438, ',': 833, '.': 833, '/': 1589,
        '~': 1998, '!': 927, '@': 2221, '#': 1998, '$': 1638, '%': 2931, '^': 1998, '&': 1987, '*': 1638, '(': 1252, ')': 1252, '_': 1638, '+': 1998,
        'Q': 1959, 'W': 2600, 'E': 1574, 'R': 1803, 'T': 1571, 'Y': 1698, 'U': 1871, 'I': 1258, 'O': 1959, 'P': 1689, '{': 1638, '}': 1638, '|': 1252,
        'A': 1789, 'S': 1637, 'D': 1913, 'F': 1499, 'G': 1869, 'H': 1930, 'J': 1279, 'K': 1777, 'L': 1468, ':': 927, '"': 1354,
        'Z': 1594, 'X': 1760, 'C': 1668, 'V': 1760, 'B': 1755, 'N': 1951, 'M': 2184, '<': 1998, '>': 1998, '?': 1421,
        ' ': 788, '\t': 2304, 'avg': 1585,
    },
    'verdana italic': {
        '`': 1465, '1': 1465, '2': 1465, '3': 1465, '4': 1465, '5': 1465, '6': 1465, '7': 1465, '8': 1465, '9': 1465, '0': 1465, '-': 1047, '=': 1886,
        'q': 1436, 'w': 1886, 'e': 1373, 'r': 984, 't': 908, 'y': 1362, 'u': 1458, 'i': 633, 'o': 1399, 'p': 1436, '[': 1047, ']': 1047, '\\': 1047,
        'a': 1384, 's': 1201, 'd': 1436, 'f': 810, 'g': 1432, 'h': 1458, 'j': 793, 'k': 1353, 'l': 633, ';': 1047, "'": 619,
        'z': 1211, 'x': 1364, 'c': 1201, 'v': 1362, 'b': 1436, 'n': 1458, 'm': 2242, ',': 838, '.': 838, '/': 1047,
        '~': 1886, '!': 907, '@': 2304, '#': 1886, '$': 1465, '%': 2480, '^': 1886, '&': 1674, '*': 1465, '(': 1047, ')': 1047, '_': 1465, '+': 1886,
        'Q': 1814, 'W': 2282, 'E': 1457, 'R': 1602, 'T': 1420, 'Y': 1418, 'U': 1687, 'I': 970, 'O': 1814, 'P': 1390, '{': 1463, '}': 1463, '|': 1047,
        'A': 1573, 'S': 1575, 'D': 1764, 'F': 1324, 'G': 1787, 'H': 1732, 'J': 1048, 'K': 1597, 'L': 1283, ':': 1047, '"': 1058,
        'Z': 1579, 'X': 1579, 'C': 1609, 'V': 1573, 'B': 1580, 'N': 1724, 'M': 1942, '<': 1886, '>': 1886, '?': 1257,
        ' ': 810, '\t': 2304, 'avg': 1417,
    },
}

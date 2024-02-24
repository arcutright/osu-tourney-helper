from __future__ import annotations
import os
import sys
import re
import logging
import traceback
import threading
from typing import Final, Callable, TextIO
import subprocess
import shutil

log: Final[logging.Logger] = logging.getLogger(__name__)

# -------------------------------------------------------------
#  wrappers to enable "the console to stay at the bottom"
#  even while other things write to stdout

class ConsoleStreamHandler(logging.StreamHandler):
    def __init__(self):
        logging.StreamHandler.__init__(self)
        self.stream = TextIO() # dummy stream target like /dev/null
        self.msg_colors = {
            logging.NOTSET: '\033[38;5;45m', # blue
            logging.DEBUG: '\033[38;5;45m', # blue
            logging.INFO: '\033[38;5;247m', # gray
            logging.WARNING: '\033[38;5;227m', # yellow
            logging.ERROR: '\033[38;5;160m', # red
            logging.CRITICAL: '\033[38;5;196m', # bright red
        }
        self.reset_color = '\033[39m' # 39m = reset foreground color only

    def emit(self, record: logging.LogRecord) -> None:
        try:
            color = self.msg_colors.get(record.levelno, '')
            Console.writeln(f"{color}{self.format(record).rstrip()}{self.reset_color}")
        except Exception:
            color = '\033[38;5;165m' # purple-ish
            Console.writeln(f"{color}Unhandled exception! {traceback.format_exc().rstrip()}{self.reset_color}")

def setup_logging(level=logging.INFO):
    """ Redirect the log target in this file to go through the Conosle wrappers and color-code them """
    stdoutHandler = ConsoleStreamHandler()
    stdoutHandler.setLevel(level)
    logging.basicConfig(handlers=[stdoutHandler],
                        level=level,
                        format="%(asctime)s [%(levelname)s] %(module)s - %(funcName)s: %(message)s",
                        datefmt='%Y-%m-%d %H:%M:%S')
                        # format="%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s")


class Console:
    """ All the _prefixed methods are for writing console prompts.
    They exist so that as other write calls come in, the console prompt
    will be cleared, the other writes go out, and then the console propmt
    is re-written. This makes it feel like the conosle prompt "floats" at 
    the bottom of the console.
    """
    _LOCK = threading.RLock()
    """Used to ensure the console vs user outputs are kept separate in the `Console.*write*` funcs and `LockedWriter` """
    _last_source = '' # blank = not the console, 'c' = the console
    _console_stdout: list[str] = []
    _console_stderr: list[str] = []
    enable_colors = True
    # callbacks
    get_console_prompt: Callable[[],list[str]] | None = None
    """Returns `list(str)`: the console prompt as char array"""
    get_console_stdout: Callable[[],list[str]] | None = None
    """Returns `list(str)`: the console prompt + user input on stdin as char array"""
    get_console_stderr: Callable[[],list[str]] | None = None
    """Returns `list(str)`: the console prompt + user input on stderr as char array"""
    get_console_cursor_offset: Callable[[],int] | None = None
    """Returns `int`: the position of cursor relative to the start of the console prompt + user input (including offset due to console propmt)"""
    
    # TODO: improve foreground colors, add background colors
    _fg_colors = {
        'blue': '\033[38;5;45m', # blue
        'gray': '\033[38;5;247m', # gray
        'yellow': '\033[38;5;227m', # yellow
        'red': '\033[38;5;160m', # red
        'bright-red': '\033[38;5;196m', # bright red
    }
    _bg_colors = {}

    @staticmethod
    def _get_console_prompt():
        if callable(Console.get_console_prompt):
            propmt = Console.get_console_prompt()
        else:
            propmt = Console._console_stdout # ?
        return Console.remove_escapes(''.join(propmt))

    @staticmethod
    def _get_console_stdout():
        if callable(Console.get_console_stdout):
            stdout = Console.get_console_stdout()
        else:
            stdout = Console._console_stdout
        return Console.remove_escapes(''.join(stdout))
    
    @staticmethod
    def _get_console_stderr():
        if callable(Console.get_console_stderr):
            stderr = Console.get_console_stderr()
        else:
            stderr = Console._console_stderr
        return Console.remove_escapes(''.join(stderr))

    @staticmethod
    def _get_console_cursor_offset():
        if callable(Console.get_console_cursor_offset):
            return Console.get_console_cursor_offset()
        
        # estimate: assume we're at the end of the console stdout
        console_output = Console._get_console_stdout()
        return Console.len(console_output)
    
    @staticmethod
    def flush():
        """Flush sys.stdout with lock"""
        with Console._LOCK:
            sys.stdout.flush()

    @staticmethod
    def flush_stderr():
        """Flush sys.stderr with lock"""
        with Console._LOCK:
            sys.stderr.flush()
            
    # https://stackoverflow.com/questions/4842424/list-of-ansi-color-escape-sequences
    # https://gist.github.com/fnky/458719343aabd01cfb17a3a4f7296797
    # reset all modes: \033[0m
    # \033[31;1;4   31=red, 1=bold, 4=underline
    __ansi_color_regex = re.compile(r"\033\[\d+(?:;\d+)*[mM]")
    __ansi_escape_regex = re.compile(r"\033\[\d+(?:;\d+)*[a-ln-zA-LN-Z]")

    @staticmethod
    def remove_escapes(text: str):
        """Removes ansi escape sequences (besides colors) and unprintable zero-width chars"""
        # \a: terminal bell
        # \b: backspace
        # \v: vertical tab
        # \0: null
        # \177: delete
        return (Console.__ansi_escape_regex.sub('', text)
                .replace('\a', '').replace('\b', '').replace('\r', '').replace('\v', '')
                .replace('\0', '').replace('\177', ''))
    
    @staticmethod
    def remove_escapes_and_colors(text: str):
        """Removes ansi escape sequences (besides colors) and unprintable zero-width chars"""
        return Console.__ansi_color_regex.sub('', Console.remove_escapes(text))

    @staticmethod
    def len(text: str):
        """Length of str, ignoring ansi escape sequences and unprintable zero-width chars"""
        return len(Console.remove_escapes_and_colors(text))
    
    @staticmethod
    def get_cursor_pos():
        """Returns (column#, line#) of current cursor pos in the terminal (note this should never return col=0, only col=1). \n
        Returns (-1, -1) if there was some problem (this will try at most twice).

        WARNING 1: this will cause problems if you haven't set up stdin/stdout via `try_patch_stdin_stdout_behavior` \n
        WARNING 2: this can (unlikely) behave unpredictably if the user is typing quickly while this function is running \n
        """
        with Console._LOCK:
            (col, row) = Console.__get_cursor_pos(warn=False)
            if col < 0 or row < 0:
                (col, row) = Console.__get_cursor_pos(warn=True) # try again just in case
            return (col, row)

    @staticmethod
    def __get_cursor_pos(warn=True):
        try:
            # This code writes the ANSI escape sequence for 'what is my cursor pos?'
            # This has lots of checks and fallback logic because it is possible for
            # the user to submit keystrokes while we are reading the ANSI cursor
            # position response on stdin
            #
            # Note: ESC is written as a placeholder for '\033'

            (maxcols, maxrows) = shutil.get_terminal_size()
            minlen = 6 # len('\033[1;1R')
            maxlen = len(f"\033[{maxcols};{maxrows}R") + 5  # pretty unreasonable for user to sneak 5 keystrokes in during ANSI command echo

            sys.__stdin__.flush()
            sys.__stdout__.flush()
            sys.__stdout__.write("\033[6n") # terminal will respond with 'ESC[<row>;<col>R'
            sys.__stdout__.flush()

            # read the terminal's response (must be >= minlen)
            bufstr = sys.__stdin__.read(minlen)
            buffer = list(bufstr)
            if len(buffer) < minlen:
                sys.__stdout__.write(bufstr) # something strange happened
                return (-1, -1)
            
            ch = buffer[-1]
            j = len(buffer)
            while ch != 'R' and j < maxlen: # sanity limit in case of problems
                ch = sys.__stdin__.read(1)
                # TODO: need to figure out how to distinguish other user input at the same time, and echo it out...
                buffer.append(ch)
                j += 1

            if ch != 'R':
                if warn: print(f"Unable to read respstr end: {buffer}")
                return (-1, -1)

            respstr = ''.join(buffer[:-1]) # skip 'R' at the end
            i = respstr.rfind('\033') # find the start of the response
            if i == -1:
                sys.__stdout__.write(''.join(buffer)) # the whole thing was user chars
                if warn: print(f"Unable to read respstr start: {buffer}")
                return (-1, -1)
            elif i > 0:
                sys.__stdout__.write(''.join(buffer[0:i])) # there were user chars mixed in the front

            sep = respstr.rfind(';', i+3) # i+3: skip 'ESC[' and first digit (row#), find the row/col separator ';'
            if sep == -1:
                if warn: print(f"Unable to read respstr sep: {buffer}")
                return (-1, -1)

            row = int(respstr[i+2:sep]) # i+2: skip 'ESC['
            col = int(respstr[sep+1:])
            return (col, row)
        except Exception:
            if warn: print(f"Error reading respstr: {buffer}\n{traceback.format_exc()}")
            return (-1, -1)

    @staticmethod
    def _format_text(text: str):
        if not text:
            return ''
        elif Console.enable_colors:
            return text
        else:
            return Console.__ansi_color_regex.sub('', text)

    @staticmethod
    def __writeln(target_name: str, line: str, source: str = '', fg: str | None = None, bg: str | None = None):
        fg = Console._fg_colors.get(fg, '')
        bg = Console._bg_colors.get(bg, '')
        if fg or bg:
            line = ''.join((fg, bg, line, '\033[0m')) # TODO: maybe 'reset all' is wrong here...
        with Console.LockedWriter(target_name, source) as writer:
            writer.writeln(line)
    
    @staticmethod
    def __console_write(target_name: str, text: str):
        with Console._LOCK:
            text = Console._format_text(text)
            if target_name == 'stderr':
                target = sys.stderr
                console_buffer = Console._console_stderr
            else:
                target = sys.stdout
                console_buffer = Console._console_stdout
            
            if Console._last_source != 'c':
                bufstr = ''.join(console_buffer)
                if Console.len(bufstr) > 0:
                    # rewrite the prev console buffer on a new line
                    target.write('\n')
                    target.write(bufstr)
                else:
                    # move to a new line if we aren't at the start of a line
                    (col, row) = Console.get_cursor_pos()
                    if col > 1:
                        target.write('\n')

            target.write(text)
            #target.flush()

            # update buffer
            if not text.endswith('\n'):
                escaped_text = Console.remove_escapes(text)
                console_buffer.append(escaped_text)
            else:
                console_buffer.clear()
            
            # try to handle some ansi control sequences
            bufstr = ''.join(console_buffer).lower()
            # i = max(bufstr.rfind('\033k'), bufstr.rfind('\0330k')) # clear from cursor to end of line
            if bufstr.endswith(('\0331k','\0332k')): # clear to end of line, clear whole line
                console_buffer.clear()
                                     
            Console._last_source = 'c'

    @staticmethod
    def writeln(text: str, fg: str | None = None, bg: str | None = None):
        Console.__writeln('stdout', text, fg=fg, bg=bg)

    @staticmethod
    def writeln_stderr(text: str, fg: str | None = None, bg: str | None = None):
        Console.__writeln('stderr', text, fg=fg, bg=bg)

    @staticmethod
    def _writeln(text: str, fg: str | None = None, bg: str | None = None):
        Console.__writeln('stdout', text, 'c', fg=fg, bg=bg)

    @staticmethod
    def _writeln_stderr(text: str, fg: str | None = None, bg: str | None = None):
        Console.__writeln('stderr', text, source='c', fg=fg, bg=bg)

    @staticmethod
    def _write(text: str):
        Console.__console_write('stdout', text)
    
    @staticmethod
    def _write_stderr(text: str):
        Console.__console_write('stderr', text)

    @staticmethod
    def erase_console_output(target_name = 'stdout'):
        """Erase all of the output that was written to the console target"""
        with Console._LOCK:
            if Console._last_source != 'c':
                return
            if target_name == 'stderr':
                console_output = Console._get_console_stderr()
                target = sys.stderr
            else:
                console_output = Console._get_console_stdout()
                target = sys.stdout
            console_output_len = Console.len(console_output)
            if console_output_len > 0:
                # move to start of console output
                cursor_offset = Console._get_console_cursor_offset()
                Console.move_cursor_left_relative(cursor_offset, cursor_offset, target_name)
                # erase all console output
                (maxcols, maxrows) = shutil.get_terminal_size()
                num_lines = 1 + console_output_len // maxcols # total number of lines taken up by console output
                for i in range(num_lines, 1, -1):
                    target.write('\r') # go to beginning of line
                    target.write('\033[2K') # erase line
                    target.write('\033[1A') # up 1 line
            # erase first line
            target.write('\r') # go to beginning of line
            target.write('\033[2K') # erase line
            target.write('\033[0m') # reset all modes
            # target.flush()
        
    @staticmethod
    def move_cursor_left_relative(n: int, cursor_offset: int, target_name='stdout'):
        """Move console cursor left `n` positions, relative to its given position, wrapping up lines if needed. \n
        params:
            `cursor_col_offset`: number of positions the cursor is from the first column for the current
            line of text, ignoring any wrapping. If the text spans multiple lines, the cursor_col_offset
            should be `nrows*<max cols per row> + current col`.
        """
        n = min(n, cursor_offset) # prevent negative position
        if n == 0:
            return
        elif n < 0: 
            Console.move_cursor_right_relative(-n, cursor_offset, target_name)
            return
        with Console._LOCK:
            target = sys.stderr if target_name == 'stderr' else sys.stdout
            (maxcols, maxrows) = shutil.get_terminal_size()

            cursor_row_offset, cursor_offset = divmod(cursor_offset, maxcols)
            col_target = cursor_offset - n
            if col_target >= 0:
                target.write(f"\033[{n}D") # move cursor left
            else:
                nrows, ncols = divmod(-col_target, maxcols)
                nrows += 1
                target.write(f"\033[{nrows}A") # move cursor up
                target.write("\r") # move cursor to beginning of line
                if maxcols - ncols > 0:
                    target.write(f"\033[{maxcols - ncols}C") # move cursor right
        
    @staticmethod
    def move_cursor_right_relative(n: int, cursor_offset: int, target_name='stdout'):
        """Move console cursor right `n` positions, relative to its given position, wrapping up lines if needed. \n
        Note: this has no way of knowing the max buffer size (if moving within a line of text), so it will go
        'out of bounds' if you ask it to.

        params:
            `cursor_col_offset`: number of positions the cursor is from the first column for the current
            line of text, ignoring any wrapping. If the text spans multiple lines, the cursor_col_offset
            should be `nrows*<max cols per row> + current col`.
        """
        if n == 0:
            return
        elif n < 0:
            Console.move_cursor_left_relative(-n, cursor_offset, target_name)
            return
        with Console._LOCK:
            target = sys.stderr if target_name == 'stderr' else sys.stdout
            (maxcols, maxrows) = shutil.get_terminal_size()

            cursor_row_offset, cursor_offset = divmod(cursor_offset, maxcols)
            col_target = cursor_offset + n
            if col_target < maxcols:
                target.write(f"\033[{n}C") # move cursor right
            else:
                row_target, col_target = divmod(col_target, maxcols)
                nrows = row_target - cursor_row_offset
                target.write(f"\033[{nrows}B") # move cursor down
                target.write("\r") # move cursor to beginning of line
                if col_target > 0:
                    target.write(f"\033[{col_target}C") # move cursor right

    @staticmethod
    def try_move_cursor_left(n: int, target_name='stdout') -> bool:
        """(May be unreliable) try to move console cursor left `n` positions, wrapping up lines if needed.

        - If you set `Console.get_console_cursor_offset`, this will call `move_cursor_right_relative` and be reliable.
        - Otherwise, see the warnings for `Console.get_cursor_pos()`, which relies on reading from stdin while user may be typing.
        - Use `move_cursor_left_relative` for a reliable implementation.

        Note this does not keep track of the cursor, so it will go 'out of bounds' if you ask it to.
        """
        if n == 0:
            return True
        elif callable(Console.get_console_cursor_offset):
            Console.move_cursor_left_relative(n, Console.get_console_cursor_offset(), target_name)
            return True
        elif n < 0:
            return Console.try_move_cursor_right(-n, target_name)
        with Console._LOCK:
            target = sys.stderr if target_name == 'stderr' else sys.stdout
            (maxcols, maxrows) = shutil.get_terminal_size()
            (col, row) = Console.get_cursor_pos()
            if col < 0 or row < 0:
                return False
            nr, nc = divmod(n, maxcols)
            dest_row = row - nr
            dest_col = col - nc
            if dest_col < 1: # leftmost column = 1, not 0
                dest_row -= 1
                dest_col += maxcols
            dest_row = max(1, dest_row)
            dest_col = max(1, dest_col)
            target.write(f"\033[{dest_row};{dest_col}H") # move cursor to final row of console output
            return True

    @staticmethod
    def try_move_cursor_right(n: int, target_name='stdout') -> bool:
        """(May be unreliable) try to move console cursor right `n` positions, wrapping down lines if needed.

        - If you set `Console.get_console_cursor_offset`, this will call `move_cursor_right_relative` and be reliable.
        - Otherwise, see the warnings for `Console.get_cursor_pos()`, which relies on reading from stdin while user may be typing.
        - Use  `move_cursor_right_relative` for a reliable implementation.

        Note this does not keep track of the cursor, so it will go 'out of bounds' if you ask it to.
        """
        if n == 0:
            return True
        elif callable(Console.get_console_cursor_offset):
            Console.move_cursor_right_relative(n, Console.get_console_cursor_offset(), target_name)
            return True
        elif n < 0: 
            return Console.try_move_cursor_left(-n, target_name)
        with Console._LOCK:
            target = sys.stderr if target_name == 'stderr' else sys.stdout
            (maxcols, maxrows) = shutil.get_terminal_size()
            (col, row) = Console.get_cursor_pos()
            if col < 0 or row < 0:
                return False
            nr, nc = divmod(n, maxcols)
            dest_row = row + nr
            dest_col = col + nc
            if dest_col > maxcols:
                dest_row += 1
                dest_col -= maxcols
            dest_row = max(1, dest_row)
            dest_col = max(1, dest_col)
            target.write(f"\033[{dest_row};{dest_col}H") # move cursor to final row of console output
            return True


    class LockedWriter(object):
        """ Disposable wrapper for grabbing the console lock and doing a block of writes.
        This is to avoid constantly rewriting the 'console prompt' after each write,
        which would happen if you use Console.writeln()

        usage:
          with LockedWriter() as writer:
              writer.writeln('line 1');
               writer.writeln('line 2');
        """
        def __init__(self, target_name='stdout', source=''):
            """
            arguments:
              target_name: 'stderr' -> sys.stderr, else -> sys.stdout
              source: 'c' -> 'the console prompt', either writing the conosle prompt itself or spitting out user keystrokes
                       else -> not the console prompt. after this writer is finished, it will ensure the block of writes
                               ends with a \\n and then will write out the console prompt (+ any in-progress user input)
            """
            self.target_name = target_name
            if target_name == 'stderr':
                self.target = sys.stderr
                self.console_buffer = Console._console_stderr
            else:
                self.target = sys.stdout
                self.console_buffer = Console._console_stdout
            self.source = source
            self.prev_console_output = ''
            self.last_source = ''
            self.last_text = ''
            self.wrote_newline = False

        def __enter__(self):
            Console._LOCK.acquire()
            self.last_source = Console._last_source
            if self.last_source == self.source == 'c':
                return self
            if self.target_name == 'stderr':
                self.prev_console_output = Console._get_console_stderr()
            else:
                self.prev_console_output = Console._get_console_stdout()
                if not self.prev_console_output:
                    self.prev_console_output = Console._get_console_prompt()
                    # self.console_buffer.extend(list(self.prev_console_output))
            Console.erase_console_output(self.target_name)
            return self

        def writeln(self, text: str):
            """ write a line of text (and adds \\n if text does not end with \\n) """
            text = Console._format_text(text)
            if not text.rstrip(' \t\b').endswith('\n'):
                text += '\n'
            self.target.write(text)
            self.last_text = text
            if self.source == 'c':
                self.console_buffer.clear()

        def write(self, text: str):
            """ write text, ignoring newlines (does not add or remove \\n) """
            text = Console._format_text(text)
            self.target.write(text)
            self.last_text = text
            if self.source == 'c':
                if not text.endswith('\n'):
                    escaped_text = Console.remove_escapes(text)
                    self.console_buffer.append(escaped_text)
                else:
                    self.console_buffer.clear()

        def __exit__(self, *args):
            rewrote_console_output = False
            try:
                last_text_endswith_newline = self.last_text.rstrip(' \t\b').endswith('\n')
                if not (self.last_source == self.source == 'c'):
                    if not last_text_endswith_newline:
                        self.target.write('\n')
                    prev_console_output = Console._format_text(self.prev_console_output).rstrip('\n')
                    if Console.len(prev_console_output) > 0:
                        self.target.write(prev_console_output)
                        rewrote_console_output = True
                if self.source == 'c':
                    if last_text_endswith_newline:
                        self.console_buffer.clear()
                self.target.flush()
            finally:
                Console._last_source = 'c' if rewrote_console_output else self.source
                Console._LOCK.release()


    @staticmethod
    def test_colors():
        """ Test the ansi color sequences and print instructions for the escape sequences """
        with Console.LockedWriter() as w:
            prev_enable_colors = Console.enable_colors
            Console.enable_colors = True
            # https://stackoverflow.com/questions/4842424/list-of-ansi-color-escape-sequences
            # https://gist.github.com/fnky/458719343aabd01cfb17a3a4f7296797
            w.writeln("\\033[**m  general format, ** = number or semicolon-delimited numbers")
            w.writeln('\\033[0m   reset everything to defaults')
            w.writeln('** = 30-37 and 90-97 are foreground colors (4-bit)')
            w.writeln('** = 40-47 and 100-107 are background colors (4-bit)')
            w.writeln('below you can see examples of the 4-bit color escape sequences')
            def ansi_color(*vals):
                vals_str = ';'.join(str(v) for v in vals)
                return f"\033[{vals_str}m" + f"\\033[{vals_str}m" + f"\033[0m"
            for i in range(30, 37+1):
                j = 97 if i < 35 else 30 # 97 = bright white fg, 30 = black fg
                w.writeln('\t'.join([ansi_color(i), ansi_color(i+60), ansi_color(i+10,j), ansi_color(i+70,j)]))
            w.writeln("\\033[<L>;<C>H OR \\033[<L>;<C>f  puts the cursor at line L and column C.")
            w.writeln("\\033[<N>A  Move the cursor up N lines")
            w.writeln("\\033[<N>B  Move the cursor down N lines")
            w.writeln("\\033[<N>C  Move the cursor right N columns")
            w.writeln("\\033[<N>D  Move the cursor left N columns")
            w.writeln("\\033[<N>E  Move the cursor down N lines, to beginning of line")
            w.writeln("\\033[<N>F  Move the cursor up N lines, to beginning of line")
            w.writeln("\\033[2J  Clear the screen, move to (0,0)")
            w.writeln("\\033[K   Erase from cursor to end of line (equiv to \\033[0K)")
            w.writeln("\\033[1K  Erase line from beginning to current cursor")
            w.writeln("\\033[2K  Clear line")
            w.writeln("\\033[s   Save cursor position (non-standard, may also be '\\033 7')")
            w.writeln("\\033[u   Restore cursor position (non-standard, may also be '\\033 8')")
            w.writeln(" ")
            w.writeln("\\033[1m  \033[1mSample Text\033[0m  Bold / increase intensity")
            w.writeln("\\033[2m  \033[2mSample Text\033[0m  Faint / decrease intensity (rarely supported)")
            w.writeln("\\033[3m  \033[3mSample Text\033[0m  Italic (rarely supported, sometimes treated as inverse)")
            w.writeln("\\033[4m  \033[4mSample Text\033[0m  Underline")
            w.writeln("\\033[5m  \033[5mSample Text\033[0m  Slow blink (less than 150 per minute)")
            w.writeln("\\033[6m  \033[6mSample Text\033[0m  Rapid blink (rarely supported)")
            w.writeln("\\033[7m  \033[7mSample Text\033[0m  swap foreground / background colors")
            w.writeln("\\033[8m  \033[8mSample Text\033[0m  'conceal' (rarely supported) ")
            w.writeln("\\033[9m  \033[9mSample Text\033[0m  strikethrough (rarely supported) ")
            Console.enable_colors = prev_enable_colors

    original_stdin_mode = None
    original_stdout_mode = None

    @staticmethod
    def try_patch_stdin_stdout_behavior():
        """ Try to patch stdin/stdout behavior for terminals to enable ANSI escape sequences, disable LINE/insert mode, disable ECHO, etc.

        This tries to
        - Set the virtual console mode to enable escape sequences
        - (on Windows) If that fails, tries to re-spawn this process in powershell (more likely to support colors)

        See related `try_restore_stdin_stdout_behavior`
        """
        with Console._LOCK:
            if not sys.stdout.isatty():
                return
            if Console.original_stdin_mode is not None:
                return
            
            if os.name != 'nt':
                try:
                    import termios
                    # disable ECHO and line mode for stdin
                    Console.original_stdin_mode = termios.tcgetattr(sys.stdin)
                    stdin_mode = termios.tcgetattr(sys.stdin)
                    stdin_mode[3] = stdin_mode[3] & ~(termios.ECHO | termios.ICANON)
                    termios.tcsetattr(sys.stdin, termios.TCSAFLUSH, stdin_mode)
                except Exception:
                    pass
                return
            try:
                import psutil
                parent_names = {parent.name().lower() for parent in psutil.Process().parents()}
            except Exception:
                parent_names = []

            # print(f"console parent names: [{', '.join(parent_names)}]")
            try:
                # call kernel32 SetConsoleMode() to enable ANSI escape codes and disable ECHO/insert/line mode
                import ctypes
                import ctypes.wintypes
                # https://learn.microsoft.com/en-us/windows/console/setconsolemode?redirectedfrom=MSDN#ENABLE_VIRTUAL_TERMINAL_PROCESSING
                # GetStdHandle(-10): stdin, -11 = stdout
                Console.original_stdin_mode = ctypes.wintypes.DWORD()
                Console.original_stdout_mode = ctypes.wintypes.DWORD()
                kernel32 = ctypes.windll.kernel32
                kernel32.GetConsoleMode(kernel32.GetStdHandle(-10), ctypes.byref(Console.original_stdin_mode))
                kernel32.GetConsoleMode(kernel32.GetStdHandle(-11), ctypes.byref(Console.original_stdout_mode))
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-10), 0x0200) # 0x0200 = ENABLE_VIRTUAL_TERMINAL_INPUT, 1 = ENABLE_PROCESSED_INPUT
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 4 | 2 | 1) # ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_PROCESSED_OUTPUT
            except Exception:
                if 'windowsterminal.exe' in parent_names:
                    return  # windows terminal has ANSI escape support by default
                Console.enable_colors = False
                print('---- Console colors have been disabled ---')
                print('if you want colors, run this program from Windows Terminal')
                print('(you may have to install from Microsoft Store)')
                if not (('powershell.exe' in parent_names) or ('powershell' in parent_names)):
                    print('trying to spawn in powershell...')
                    try:
                        caller = os.path.abspath(sys.executable) or ''
                        entrypoint = os.path.abspath(sys.argv[0])
                        entrypoint_basename = os.path.basename(entrypoint)
                        caller_program = os.path.splitext(os.path.basename(caller) or '')[0].lower()
                        file_ext = (os.path.splitext(entrypoint)[1] or '').lower()
                        print(f'caller: {caller}')
                        print(f'entry : {entrypoint_basename}')

                        # windows terminal (if installed): %LocalAppData%\Microsoft\WindowsApps\wt.exe

                        # note: this fallback approach probably won't help
                        # - for some goofy reason, in powershell, it only supports ansi escapes for _itself_ by default
                        #   I read that sometimes you can cheat by doing '(.\test.exe)', not '& test.exe', but this is untested.
                        if caller and ('python' in caller_program or 'pypy' in caller_program or file_ext in ('.py', '.py3')):
                            p = subprocess.Popen(f'start powershell.exe -command "& """{caller}""" {entrypoint_basename}"', cwd=os.getcwd(), shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        else:
                            p = subprocess.Popen(f'start powershell.exe -command "& """{entrypoint_basename}"""', cwd=os.getcwd(), shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                        p.wait(2)
                        if p.returncode is None: # process didn't die, it probably spawned ok
                            Console.try_restore_stdin_stdout_behavior()
                            sys.exit(0)
                    except Exception:
                        traceback.print_exc()

    @staticmethod
    def try_restore_stdin_stdout_behavior():
        """Try to undo any changes made by `try_patch_stdin_stdout_behavior`"""
        with Console._LOCK:
            try:
                if os.name == 'nt':
                    import ctypes
                    kernel32 = ctypes.windll.kernel32
                    # GetStdHandle(-10): stdin, -11 = stdout
                    if Console.original_stdin_mode is not None:
                        kernel32.SetConsoleMode(kernel32.GetStdHandle(-10), Console.original_stdin_mode)
                    if Console.original_stdout_mode is not None:
                        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), Console.original_stdout_mode)
                else:
                    import termios
                    if Console.original_stdin_mode is not None:
                        termios.tcsetattr(sys.stdin, termios.TCSAFLUSH, Console.original_stdin_mode)
            except Exception:
                pass
            finally:
                Console.original_stdin_mode = None
                Console.original_stdout_mode = None

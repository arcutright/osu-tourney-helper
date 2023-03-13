import os
import sys
import re
import logging
import traceback
from multiprocessing import Lock, RLock
from typing import Final, Callable, Union
import ctypes
import subprocess
import shutil

log: Final[logging.Logger] = logging.getLogger(__name__)

# -------------------------------------------------------------
#  wrappers to enable "the console to stay at the bottom"
#  even while other things write to stdout

class ConsoleStreamHandler(logging.StreamHandler):
    def __init__(self):
        logging.StreamHandler.__init__(self)
        self.stream = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            Console.writeln(f"(r) {self.format(record)}")
        except Exception:
            Console.writeln(f"(r2) {traceback.format_exc()}")

def redirect_log(level=logging.INFO):
    """ Redirect the log target in this file to go through the Conosle wrappers """
    stdoutHandler = ConsoleStreamHandler()
    stdoutHandler.setLevel(level)
    #logging.basicConfig(level=level,
    #                    format="%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s")
    log.handlers.clear()
    log.addHandler(stdoutHandler)


class Console:
    """ All the _prefixed methods are for writing console prompts.
    They exist so that as other write calls come in, the console prompt
    will be cleared, the other writes go out, and then the console propmt
    is re-written. This makes it feel like the conosle prompt "floats" at 
    the bottom of the console.
    """
    _LOCK = RLock()
    _last_source = '' # blank = not the console, 'c' = the console
    _console_stdout: "list[str]" = []
    _console_stderr: "list[str]" = []
    enable_colors = True
    # callbacks
    get_console_stdout: "Union[Callable[[],list[str]], None]" = None
    """Returns `list(str)`: the console prompt + user input on stdin as char array"""
    get_console_stderr: "Union[Callable[[],list[str]], None]" = None
    """Returns `list(str)`: the console prompt + user input on stderr as char array"""
    get_console_cursor_offset: "Union[Callable[[],int], None]" = None
    """Returns `int`: the position of cursor relative to the start of the console prompt + user input (including offset due to console propmt)"""
    
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
        Returns (-1, -1) if there was some problem.
        """
        with Console._LOCK:
            sys.stdout.write("\033[6n") # reports as ESC[#;#R
            sys.stdout.flush()
            resp = []
            ch = ''
            l = 0
            while l < 15: # sanity limit in case of problems
                ch = sys.stdin.read(1)
                if ch == 'R':
                    break
                resp.append(ch)
                l += 1
            respstr = ''.join(resp[2:])
            idx = respstr.find(';')
            if idx == -1:
                return (-1, -1)
            row = int(respstr[:idx])
            col = int(respstr[idx+1:])
            return col, row

    @staticmethod
    def _format_text(text: str):
        if not text:
            return ''
        elif Console.enable_colors:
            return text
        else:
            return Console.__ansi_color_regex.sub('', text)

    @staticmethod
    def __writeln(target_name: str, line: str, source: str = ''):
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
    def writeln(text: str):
        Console.__writeln('stdout', text)

    @staticmethod
    def writeln_stderr(text: str):
        Console.__writeln('stderr', text)

    @staticmethod
    def _writeln(text: str):
        Console.__writeln('stdout', text, 'c')

    @staticmethod
    def _writeln_stderr(text: str):
        Console.__writeln('stderr', text, 'c')

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
                Console.move_cursor_left(cursor_offset)
                # erase all console output
                (maxcols, maxrows) = shutil.get_terminal_size()
                num_lines = 1 + console_output_len // maxcols # total number of lines taken up by console output
                for i in range(num_lines, 0, -1):
                    target.write('\r') # go to beginning of line
                    target.write('\033[2K') # erase line
                    if i > 1:
                        target.write('\033[1A') # up 1 line

    @staticmethod
    def move_cursor_left(n: int, target_name='stdout'):
        """Move console cursor left `n` positions, wrapping up lines if needed. \n
        Note this does not keep track of the cursor, so it will go 'out of bounds' if you ask it to.
        """
        if n == 0:
            return
        elif n < 0: 
            return Console.move_cursor_right(-n, target_name)
        with Console._LOCK:
            target = sys.stderr if target_name == 'stderr' else sys.stdout
            (maxcols, maxrows) = shutil.get_terminal_size()
            (col, row) = Console.get_cursor_pos()
            nr, nc = divmod(n, maxcols)
            dest_row = row - nr
            dest_col = col - nc
            if dest_col < 1: # leftmost column = 1, not 0
                dest_row -= 1
                dest_col += maxcols
            dest_row = max(1, dest_row)
            dest_col = max(1, dest_col)
            target.write(f"\033[{dest_row};{dest_col}H") # move cursor to final row of console output

    @staticmethod
    def move_cursor_right(n: int, target_name='stdout'):
        """Move console cursor right `n` positions, wrapping down lines if needed. \n
        Note this does not keep track of the cursor, so it will go 'out of bounds' if you ask it to.
        """
        if n == 0:
            return
        elif n < 0: 
            return Console.move_cursor_left(-n, target_name)
        with Console._LOCK:
            target = sys.stderr if target_name == 'stderr' else sys.stdout
            (maxcols, maxrows) = shutil.get_terminal_size()
            (col, row) = Console.get_cursor_pos()
            nr, nc = divmod(n, maxcols)
            dest_row = row + nr
            dest_col = col + nc
            if dest_col > maxcols:
                dest_row += 1
                dest_col -= maxcols
            dest_row = max(1, dest_row)
            dest_col = max(1, dest_col)
            target.write(f"\033[{dest_row};{dest_col}H") # move cursor to final row of console output


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
            try:
                last_text_endswith_newline = self.last_text.rstrip(' \t\b').endswith('\n')
                if not (self.last_source == self.source == 'c'):
                    if not last_text_endswith_newline:
                        self.target.write('\n')
                    prev_console_output = Console._format_text(self.prev_console_output).rstrip()
                    if Console.len(prev_console_output) > 0:
                        self.target.write(prev_console_output)
                if self.source == 'c':
                    if last_text_endswith_newline:
                        self.console_buffer.clear()
                self.target.flush()
            finally:
                Console._last_source = self.source
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

    @staticmethod
    def try_fix_colors_for_cmd():
        """ If this process was spawned by cmd/powershell (only affects windows hosts),
        this will try to set the console mode to enable ANSI escape sequences, which are
        not supported by default but can be via a kernel32 call (or a registry flag).

        This tries to
        - Set the virtual console mode to enable escape sequences
        - If that fails, tries to re-spawn this process in powershell (more likely to support colors)
        """
        if not sys.stdout.isatty() or os.name != 'nt':
            return
        try:
            import psutil
            parent_names = {parent.name().lower() for parent in psutil.Process().parents()}
        except Exception:
            parent_names = []

        # print(f"console parent names: [{', '.join(parent_names)}]")
        if 'windowsterminal.exe' in parent_names:
            # windows terminal has proper support for ANSI escape codes
            return
        if not parent_names or any(shell in parent_names for shell in ['cmd.exe', 'powershell.exe', 'conhost.exe']):
            try:
                # dirty hack: call kernel32 SetConsoleMode()
                # https://learn.microsoft.com/en-us/windows/console/setconsolemode?redirectedfrom=MSDN#ENABLE_VIRTUAL_TERMINAL_PROCESSING
                # GetStdHandle(-11): grab conhost
                # 7 = ENABLE_VIRTUAL_TERMINAL_PROCESSING | ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_PROCESSED_OUTPUT
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except Exception:
                Console.enable_colors = False
                print('---- Console colors have been disabled for cmd/powershell ---')
                print('if you want colors, run this program from Windows Terminal')
                print('(you may have to install from Microsoft Store)')
                if 'powershell.exe' not in parent_names:
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
                            exit(0)
                    except Exception:
                        traceback.print_exc()

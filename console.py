import os
import sys
import re
import logging
import traceback
from multiprocessing import Lock
from typing import TextIO, Final
import ctypes
import subprocess
import psutil

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
    _LOCK = Lock()
    _last_source = '' # blank = not the console, 'c' = the console
    _console_stdout = []
    _console_stderr = []
    enable_colors = True
    
    @staticmethod
    def flush():
        with Console._LOCK:
            sys.stdout.flush()

    @staticmethod
    def flush_stderr():
        with Console._LOCK:
            sys.stderr.flush()
            
    # https://stackoverflow.com/questions/4842424/list-of-ansi-color-escape-sequences
    # reset color: \033[0m
    # \033[31;1;4   31=red, 1=bold, 4=underline
    __ansi_escape_regex = re.compile(r"\033\[\d+(?:;\d+)*m")

    @staticmethod
    def _format_text(text: str):
        if Console.enable_colors:
            return text
        else:
            return Console.__ansi_escape_regex.sub('', text)

    @staticmethod
    def __writeln(target_name: str, line: str, source: str = ''):
        with Console.LockedWriter(target_name, source) as writer:
            writer.writeln(line)
    
    @staticmethod
    def __console_write(target_name: str, text: str):
        with Console._LOCK:
            text = Console._format_text(text)
            if target_name == 'stderr':
                sys.stderr.write(text)
                #sys.stderr.flush()
                if not text.endswith('\n'):
                    Console._console_stderr.append(text)
                else:
                    Console._console_stderr.clear()
            else:
                sys.stdout.write(text)
                #sys.stdout.flush()
                if not text.endswith('\n'):
                    Console._console_stdout.append(text)
                else:
                    Console._console_stderr.clear()
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
            self.target = sys.stderr if target_name == 'stderr' else sys.stdout
            self.source = source
            self.prev_console_output = ''
            self.last_source = ''
            self.last_text = ''
            self.wrote_newline = False
        
        @staticmethod
        def __count_console_chars(line: str):
            return sum(-1 if ch == '\b' else (0 if ch in ('\n', '\r') else 1) for ch in line)

        def __enter__(self):
            Console._LOCK.acquire()
            self.last_source = Console._last_source
            if self.last_source == self.source == 'c':
                return self
            if self.target_name == 'stderr':
                self.prev_console_output = ''.join(Console._console_stderr)
            else:
                self.prev_console_output = ''.join(Console._console_stdout)
            n = len(self.prev_console_output)
            if n > 0:
                # erase the conosle output by overwriting it with space chars
                self.target.write(' '*n)
                self.target.write('\r')
            return self

        def writeln(self, text: str):
            """ write a line of text (and adds \\n if text does not end with \\n) """
            text = Console._format_text(text)
            if not text.rstrip(' \t\b').endswith('\n'):
                text += '\n'
            self.target.write(text)
            self.last_text = text

        def write(self, text: str):
            """ write text, ignoring newlines (does not add or remove \\n) """
            text = Console._format_text(text)
            self.target.write(text)
            self.last_text = text

        def __exit__(self, *args):
            last_text_endswith_newline = self.last_text.rstrip(' \t\b').endswith('\n')
            if not (self.last_source == self.source == 'c'):
                if not last_text_endswith_newline:
                    self.target.write('\n')
                prev_console_output = Console._format_text(self.prev_console_output).rstrip()
                self.target.write(prev_console_output)
            if self.source == 'c' and last_text_endswith_newline:
                if self.target_name == 'stderr':
                    Console._console_stderr.clear()
                else:
                    Console._console_stdout.clear()
            self.target.flush()
            Console._last_source = self.source
            Console._LOCK.release()


    @staticmethod
    def test_colors():
        """ Test the ansi color sequences and print instructions for the escape sequences """
        with Console.LockedWriter() as w:
            prev_enable_colors = Console.enable_colors
            Console.enable_colors = True
            # https://stackoverflow.com/questions/4842424/list-of-ansi-color-escape-sequences
            w.writeln("\\033[**m  general format, ** = number or semicolon-delimited numbers")
            w.writeln('\\033[0m   reset everything to defaults')
            w.writeln('** = 30-37 and 90-97 are foreground colors (4-bit)')
            w.writeln('** = 40-47 and 100-107 are background colors (4-bit)')
            w.writeln('below you can see examples of the escape sequence and what it looks like')
            def ansi_esc(*vals):
                vals_str = ';'.join(str(v) for v in vals)
                return f"\033[{vals_str}m" + f"\\033[{vals_str}m" + f"\033[0m"
            for i in range(30, 37+1):
                j = 97 if i < 35 else 30 # 97 = bright white fg, 30 = black fg
                w.writeln('\t'.join([ansi_esc(i), ansi_esc(i+60), ansi_esc(i+10,j), ansi_esc(i+70,j)]))
            w.writeln("\\033[2K - Clear Line")
            w.writeln("\\033[<L>;<C>H OR \\033[<L>;<C>f puts the cursor at line L and column C.")
            w.writeln("\\033[<N>A Move the cursor up N lines")
            w.writeln("\\033[<N>B Move the cursor down N lines")
            w.writeln("\\033[<N>C Move the cursor forward N columns")
            w.writeln("\\033[<N>D Move the cursor backward N columns")
            w.writeln("\\033[2J Clear the screen, move to (0,0)")
            w.writeln("\\033[K Erase to end of line")
            w.writeln("\\033[s Save cursor position")
            w.writeln("\\033[u Restore cursor position")
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
        if not sys.stdout.isatty() or os.name != 'nt': return
        parent_names = {parent.name().lower() for parent in psutil.Process().parents()}
        # print(f"console parent names: [{', '.join(parent_names)}]")
        if 'windowsterminal.exe' in parent_names:
            # windows terminal has proper support for ANSI escape codes
            return
        if 'cmd.exe' in parent_names or 'powershell.exe' in parent_names or 'conhost.exe' in parent_names:
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

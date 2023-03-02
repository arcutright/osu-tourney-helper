import os
import sys
import logging
import traceback
from multiprocessing import Lock
from typing import TextIO, Final

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

    @staticmethod
    def flush():
        with Console._LOCK:
            sys.stdout.flush()

    @staticmethod
    def flush_stderr():
        with Console._LOCK:
            sys.stderr.flush()


    @staticmethod
    def __writeln(target_name: str, line: str, source: str = ''):
        with Console.LockedWriter(target_name, source) as writer:
            writer.writeln(line)
    
    @staticmethod
    def __console_write(target_name: str, text: str):
        if text.endswith('\n'):
            Console.__writeln(target_name, text, 'c')
        else:
            with Console._LOCK:
                if target_name == 'stderr':
                    sys.stderr.write(text)
                    Console._console_stderr.append(text)
                else:
                    sys.stdout.write(text)
                    Console._console_stdout.append(text)
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
        Console.__writeln(sys.stderr, text, 'c')

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
            if not text.rstrip(' \t\b').endswith('\n'):
                text += '\n'
            self.target.write(text)
            self.last_text = text

        def write(self, text: str):
            """ write text, ignoring newlines (does not add or remove \\n) """
            self.target.write(text)
            self.last_text = text

        def __exit__(self, *args):
            last_text_endswith_newline = self.last_text.rstrip(' \t\b').endswith('\n')
            if not (self.last_source == self.source == 'c'):
                if not last_text_endswith_newline:
                    self.target.write('\n')
                self.target.write(self.prev_console_output.rstrip())
            if self.source == 'c' and last_text_endswith_newline:
                if self.target_name == 'stderr':
                    Console._console_stderr.clear()
                else:
                    Console._console_stdout.clear()
            Console._last_source = self.source
            Console._LOCK.release()

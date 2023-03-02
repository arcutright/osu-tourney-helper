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
    __LOCK = Lock()
    __last_source = '' # blank = not the console, 'c' = the console
    __console_stdout = []
    __console_stderr = []

    @staticmethod
    def flush():
        with Console.__LOCK:
            sys.stdout.flush()

    @staticmethod
    def flush_stderr():
        with Console.__LOCK:
            sys.stderr.flush()

    @staticmethod
    def __count_console_chars(line: str):
        return sum(-1 if ch == '\b' else (0 if ch in ('\n', '\r') else 1) for ch in line)

    @staticmethod
    def __writeln(target_name: str, line: str, next_source: str = ''):
        with Console.__LOCK:
            target = sys.stderr if target_name == 'stderr' else sys.stdout
            last_source = Console.__last_source
            if last_source == 'c' and next_source == 'c':
                target.write(line)
                if not line.endswith('\n'):
                    target.write('\n')
            else:
                if target_name == 'stderr':
                    prev_output = ''.join(Console.__console_stderr)
                else:
                    prev_output = ''.join(Console.__console_stdout)
                if len(prev_output) > 0:
                    n = Console.__count_console_chars(prev_output)
                    if n - len(line) > 0:
                        target.write(' '*n)
                    target.write('\r')
                    target.write(line)
                    if not line.endswith('\n'):
                        target.write('\n')
                    target.write(prev_output)
                else:
                    target.write(line)
                    if not line.endswith('\n'):
                        target.write('\n')
            if next_source == 'c':
                if target_name == 'stderr':
                    Console.__console_stderr.clear()
                else:
                    Console.__console_stdout.clear()
            Console.__last_source = next_source

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
    def __console_write(target_name: str, text: str):
        if text.endswith('\n'):
            Console.__writeln(target_name, text, 'c')
        else:
            with Console.__LOCK:
                if target_name == 'stderr':
                    sys.stderr.write(text)
                    Console.__console_stderr.append(text)
                else:
                    sys.stdout.write(text)
                    Console.__console_stdout.append(text)
                Console.__last_source = 'c'

    @staticmethod
    def _write(text: str):
        Console.__console_write('stdout', text)
    
    @staticmethod
    def _write_stderr(text: str):
        Console.__console_write('stderr', text)

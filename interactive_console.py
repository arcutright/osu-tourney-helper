import sys
import re
import traceback
import shutil
import multiprocessing
from multiprocessing.synchronize import Event
from typing import Union
from readchar import readkey, key as keycode
import pyperclip # clipboard support

from console import Console, log
from config import Config, parse_config
from osu_irc_bot import OsuIRCBot

class InteractiveConsole:
    def __init__(self,
                 bot: "Union[OsuIRCBot, DummyBot]",
                 cfg: Config,
                 bot_motd_event: "Union[Event, None]" = None,
                 bot_response_event: "Union[Event, None]" = None,
                 stop_event: "Union[Event, None]" = None
    ):
        self.bot = bot
        self.cfg = cfg
        self.bot_motd_event = bot_motd_event
        self.bot_response_event = bot_response_event
        self.stop_event = stop_event if stop_event is not None else multiprocessing.Event()

        self.insert_mode = False
        self.inputs_list: "list[list[str]]" = []
        self.inputs_idx = 0
        self.current_input: "list[str]" = []
        self.current_input_idx = 0
        self.prev_input_idx = 0

        (self.maxcols, self.maxrows) = shutil.get_terminal_size()
        self.console_prompt = self.get_console_prompt()
        self.console_prompt_len = Console.len(self.console_prompt)

    @staticmethod
    def __get_console_prompt(insert_mode: bool = False):
        # https://stackoverflow.com/questions/4842424/list-of-ansi-color-escape-sequences
        # https://gist.github.com/fnky/458719343aabd01cfb17a3a4f7296797
        return (
            '\033[38;5;254;'+'48;5;238;'+'4m' + # dark gray bg, light fg, underline
            '>> console (!q to quit)' +
            ('\033[0m\033[38;5;226;4m' + '*' if insert_mode else ' ') + # yellow asterisk for 'insert mode'
            '\033[0m' + # reset all modes
            ': '
        )
    
    def get_console_prompt(self):
        """Returns the console prompt message, and updates `self.console_propmt` and `self.console_prompt_len`"""
        self.console_prompt = InteractiveConsole.__get_console_prompt(self.insert_mode)
        self.console_prompt_len = Console.len(self.console_prompt)
        return self.console_prompt
    
    def erase_current_stdin(self):
        """Erase everything in stdin, excluding the console prompt.
        Will also reset `self.current_input` and `self.current_input_idx`
        """
        with Console._LOCK:
            max_idx = Console.len(''.join(self.current_input))
            if self.current_input_idx < max_idx:
                self.move_cursor_right(max_idx - self.current_input_idx)

            if max_idx + self.console_prompt_len <= self.maxcols:
                num_current_lines = 1
            else:
                chars_after_first_line = max_idx + self.console_prompt_len - self.maxcols
                num_current_lines = 2 + chars_after_first_line // self.maxcols

            for i in range(num_current_lines, 0, -1):
                sys.stdout.write('\r') # go to beginning of line
                sys.stdout.write('\033[2K') # erase line
                if i > 1:
                    sys.stdout.write('\033[1A') # up 1 line

            Console._write(self.get_console_prompt()) # in case prompt has changed
            sys.stdout.write('\033[0m') # reset all modes
            sys.stdout.write('\033[K') # erase from cursor to end of line

            self.current_input.clear()
            self.current_input_idx = 0

    def replace_current_stdin_with_history(self, w: Console.LockedWriter, history_idx: int):
        """Reset 'stdin' to show the console prompt followed by the history at `history_idx`. \n
        If `history_idx` = -1, resets it to `current_input` and will restore cursor pos. \n
        Otherwise the cursor pos will be at the end of the history value.
        """
        prev_pos = self.current_input_idx
        prev_input = self.current_input.copy()

        self.erase_current_stdin()
        self.current_input.clear()

        if history_idx == -1:
            self.current_input.extend(prev_input)
            # sys.stdout.write('\033[0m') # reset color
            w.write(''.join(prev_input))
            self.current_input_idx = len(prev_input)
            self.move_cursor_left(len(prev_input) - prev_pos)
        elif history_idx >= 0 and history_idx < len(self.inputs_list):
            self.current_input.extend(self.inputs_list[history_idx])
            # sys.stdout.write('\033[0m') # reset color
            w.write(''.join(self.current_input))
            self.current_input_idx = len(self.current_input)

    def move_cursor_left(self, n: int):
        """Move console cursor left `n` positions, wrapping up lines if needed.
        Will clamp to ensure you stay within the buffer. \n
        Note that this will mutate `self.current_input_idx`.
        """
        max_move = max(0, self.current_input_idx)
        n = min(n, max_move)
        Console.move_cursor_left(n, 'stdout')
        self.current_input_idx -= n
    
    def move_cursor_right(self, n: int):
        """Move console cursor right `n` positions, wrapping down lines if needed.
        Will clamp to ensure you stay within the buffer. \n
        Note that this will mutate `self.current_input_idx`.
        """
        max_move = max(0, len(self.current_input) - self.current_input_idx)
        n = min(n, max_move)
        Console.move_cursor_right(n, 'stdout')
        self.current_input_idx += n

    def main_loop(self):
        if self.bot_motd_event is not None:
            self.bot_motd_event.wait()
        Console.flush()
        Console.flush_stderr()
        Console.get_console_stdout = lambda: \
                                     [] if sum(len(s) for s in self.current_input) == 0 \
                                        else [self.get_console_prompt(), *self.current_input]
        Console.get_console_stderr = lambda: []
        Console.get_console_cursor_offset = lambda: self.current_input_idx + self.console_prompt_len
        
        (self.maxcols, self.maxrows) = shutil.get_terminal_size()

        # loop over lines of input
        while not self.stop_event.is_set():
            self.console_prompt = self.get_console_prompt()
            Console._write(self.console_prompt)
            self.current_input.clear()
            self.current_input_idx = 0
            self.prev_input_idx = 0

            sys.stdout.flush()
            sys.stderr.flush()
            line = self._readline()

            # do something with finished line
            if line.lower() in ('\\q', '!q', '\\quit', '!quit'):
                self.bot_response_event.clear()
                self.bot.close_room(warn=False)
                self.bot_response_event.wait(self.cfg.response_timeout)
                Console.flush()
                break
            if line:
                try:
                    self.bot_response_event.clear()
                    self.bot.send_bot_command(line)
                    self.bot_response_event.wait(self.cfg.response_timeout)
                    Console.flush()
                except Exception as ex:
                    log.error(ex, exc_info=True)
    
    def _readline(self):
        # loop over key presses during line input
        while not self.stop_event.is_set():
            ch = readkey()
            with Console.LockedWriter(source='c') as w:
                (self.maxcols, self.maxrows) = shutil.get_terminal_size()
                self.current_input_idx = max(0, min(self.current_input_idx, len(self.current_input)))

                if ch in (keycode.ENTER, keycode.CR, keycode.LF):
                    # current_input[::-1].index('')
                    self.inputs_list.append(self.current_input.copy())
                    self.inputs_idx = len(self.inputs_list)
                    w.write('\n')
                    line = ''.join(self.current_input).strip()
                    self.current_input.clear()
                    self.current_input_idx = 0
                    return line

                elif ch == keycode.UP:
                    if self.inputs_idx > 0: # >0 ensures UP never clears the console
                        self.inputs_idx -= 1
                    self.replace_current_stdin_with_history(w, self.inputs_idx)

                elif ch == keycode.DOWN:
                    if self.inputs_idx <= len(self.inputs_list)-1:
                        self.inputs_idx += 1
                    self.replace_current_stdin_with_history(w, self.inputs_idx)

                elif ch == keycode.LEFT:
                    self.move_cursor_left(1)

                elif ch == keycode.RIGHT:
                    self.move_cursor_right(1)

                elif ch == keycode.HOME:
                    if self.current_input_idx > 0:
                        # sys.stdout.write(f'\033[{self.current_input_idx}D') # backwards N columns
                        self.move_cursor_left(self.current_input_idx)
                        # Console._write('\b'*self.current_input_idx)
                        self.current_input_idx = 0

                elif ch == keycode.END:
                    if self.current_input_idx < len(self.current_input):
                        # sys.stdout.write(f'\033[{len(self.current_input) - self.current_input_idx}C') # forwards N columns
                        # Console._write(''.join(self.current_input[self.current_input_idx:]))
                        self.move_cursor_right(len(self.current_input) - self.current_input_idx)
                        self.current_input_idx = len(self.current_input)
                        
                elif ch in keycode.BACKSPACE:
                    if len(self.current_input) > 0 and self.current_input_idx > 0:
                        n = len(self.current_input) - self.current_input_idx
                        
                        self.move_cursor_left(1)
                        if n >= 0:
                            idx = self.current_input_idx
                            if n > 0:
                                w.write(''.join(self.current_input[idx+1:]))
                            w.write(' ')
                            self.current_input_idx = len(self.current_input)
                            self.move_cursor_left(n + 1)

                            # update state
                            if idx < len(self.current_input):
                                del self.current_input[idx]
                            else:
                                del self.current_input[-1]

                elif ch in keycode.DELETE:
                    n = len(self.current_input) - self.current_input_idx
                    if len(self.current_input) > 0 and n > 0:
                        # overwrite the rest of the line with chars after the 'deleted char'
                        w.write(''.join(self.current_input[self.current_input_idx+1:]))
                        w.write(' ') # the extra array index at the end of the line
                        idx = self.current_input_idx
                        # restore cursor pos
                        self.current_input_idx = len(self.current_input)
                        self.move_cursor_left(n)

                        # update state
                        if idx < len(self.current_input):
                            del self.current_input[idx]
                        else:
                            del self.current_input[-1]

                elif ch in keycode.INSERT:
                    # toggle insert mode and re-draw prompt + stdin
                    self.insert_mode = not self.insert_mode
                    self.replace_current_stdin_with_history(w, -1)

                elif ch == keycode.CTRL_V:
                    text = pyperclip.paste()
                    # TODO: what happens if someone pastes ANSI escapes?
                    # buftext = Console.remove_escapes(text)
                    if self.current_input_idx >= len(self.current_input):
                        # we are at end of input, simple append
                        w.write(text)
                        self.current_input.extend(list(text))
                        self.current_input_idx += len(text)
                    elif not self.insert_mode:
                        # paste text
                        w.write(text)
                        # re-write the right hand side (the previous text in the buffer that should come after the pasted text)
                        idx = self.current_input_idx
                        rhs = self.current_input[self.current_input_idx:]
                        w.write(''.join(rhs))
                        # restore cursor
                        self.current_input_idx = len(self.current_input) + len(text)
                        self.move_cursor_left(len(rhs))
                        # update state
                        next_input = self.current_input[:idx] + list(text) + rhs
                        self.current_input.clear()
                        self.current_input.extend(next_input)
                    else:
                        # insert mode, write all text
                        w.write(text)
                        # right hand side (if there are any chars beyond the pasted text)
                        rhs = self.current_input[self.current_input_idx + len(text):]
                        # update state
                        next_input = self.current_input[:self.current_input_idx] + list(text) + rhs
                        self.current_input.clear()
                        self.current_input.extend(next_input)
                        self.current_input_idx += len(text)

                elif ch == keycode.TAB:
                    # TODO: support tab-completion
                    continue

                elif ch == keycode.CTRL_R:
                    # TODO: support reverse-search? would be a lot of effort
                    continue

                elif ch in (keycode.ESC):
                    continue

                elif ch not in (keycode.SPACE) and (ch in keycode.__dict__.values()):
                    # other control chars and combinations
                    continue

                else:
                    # write a normal character
                    w.write(ch)
                    if self.current_input_idx >= len(self.current_input):
                        # we are at end of input, simple append
                        self.current_input.append(ch)
                        self.current_input_idx += 1
                    elif self.insert_mode:
                        # overwrite char at current pos
                        self.current_input[self.current_input_idx] = ch
                        self.current_input_idx += 1
                    else:
                        n = len(self.current_input) - self.current_input_idx
                        # re-write rest of previous input if we're not at the end of the buffer (otherwise .write() would overwrite a char)
                        w.write(''.join(self.current_input[self.current_input_idx:]))
                        # update state
                        self.current_input.insert(self.current_input_idx, ch)
                        self.current_input_idx = len(self.current_input)
                        # restore cursor pos
                        self.move_cursor_left(n)


# -------------------------------------------------------------
# dummy code to test the interactive console behavior

class DummyBot:
    def __init__(self, bot_motd_event: Event, bot_response_event: Event):
        self.bot_motd_event = bot_motd_event
        self.bot_response_event = bot_response_event
        bot_motd_event.set()

    def send_bot_command(self, msg):
        Console.writeln(f"DummyBot send_bot_command: '{msg}'")
        self.bot_response_event.set()

    def send_message(self, channel: str, content: str):
        Console.writeln(f"DummyBot send_message: '{channel}' -> '{content}'")
        self.bot_response_event.set()

    def send_pm(self, user: str, content: str):
        Console.writeln(f"DummyBot send_pm: '{user}' -> '{content}'")
        self.bot_response_event.set()

    def send_raw(self, content: str):
        Console.writeln(f"DummyBot send_raw: '{content}'")
        self.bot_response_event.set()

    def join_channel(self, channel: str):
        Console.writeln(f"DummyBot join_channel: '{channel}'")
        self.bot_response_event.set()

    def close_room(self, warn=True):
        Console.writeln(f"DummyBot close_room. warn={warn}")
        self.bot_response_event.set()

def test_interactive_console(stop_event: "Union[Event, None]" = None):
    cfg = parse_config()
    Console.enable_colors = cfg.enable_console_colors
    bot_response_event = multiprocessing.Event()
    bot_motd_event = multiprocessing.Event()
    bot = DummyBot(bot_motd_event, bot_response_event)
    iconsole = InteractiveConsole(bot, cfg, bot_motd_event, bot_response_event, stop_event)
    iconsole.main_loop()

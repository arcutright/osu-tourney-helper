from __future__ import annotations
import sys
import re
import traceback
import shutil
from threading import Event
import pyperclip # clipboard support

import readchar_extended.key as keycode
from readchar_extended import readkey
from helpers import value_or_fallback
from console import Console, log
from config import Config, parse_config
from osu_irc_bot import OsuIRCBot

class InteractiveConsole:
    def __init__(self,
                 bot: OsuIRCBot | DummyBot,
                 cfg: Config,
                 stop_event: Event | None = None
    ):
        self.bot = bot
        self.cfg = cfg
        self.stop_event = value_or_fallback(stop_event, Event())
        self._stopped = False

        self.insert_mode = False
        self.history: list[list[str]] = []
        self.history_idx = 0
        self.current_input: list[str] = []
        self.current_input_idx = 0

        (self.maxcols, self.maxrows) = shutil.get_terminal_size()
        self.console_prompt = self.get_console_prompt()
        self.console_prompt_len = Console.len(self.console_prompt)

    @staticmethod
    def __get_console_prompt(insert_mode: bool = False) -> str:
        # https://stackoverflow.com/questions/4842424/list-of-ansi-color-escape-sequences
        # https://gist.github.com/fnky/458719343aabd01cfb17a3a4f7296797
        return (
            '\033[38;5;254;'+'48;5;238;'+'4m' + # dark gray bg, light fg, underline
            '>> console (!q to quit)' +
            ('\033[0m\033[38;5;226;4m' + '*' if insert_mode else ' ') + # yellow asterisk for 'insert mode'
            '\033[0m' + # reset all modes
            ': '
        )
    
    def get_console_prompt(self) -> str:
        """Returns the console prompt message, and updates `self.console_propmt` and `self.console_prompt_len`"""
        if self._stopped: return ''
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
        elif history_idx >= 0 and history_idx < len(self.history):
            self.current_input.extend(self.history[history_idx])
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
        Console.move_cursor_left_relative(n, self.console_prompt_len + self.current_input_idx)
        self.current_input_idx -= n
    
    def move_cursor_right(self, n: int):
        """Move console cursor right `n` positions, wrapping down lines if needed.
        Will clamp to ensure you stay within the buffer. \n
        Note that this will mutate `self.current_input_idx`.
        """
        max_move = max(0, len(self.current_input) - self.current_input_idx)
        n = min(n, max_move)
        Console.move_cursor_right_relative(n, self.console_prompt_len + self.current_input_idx)
        self.current_input_idx += n

    def main_loop(self):
        if self.bot.motd_event is not None:
            self.bot.motd_event.wait(self.cfg.motd_timeout)
        if self.bot.response_event is not None:
            if self.cfg.room_name and self.cfg.room_password is not None:
                self.bot.response_event.wait(self.cfg.response_timeout) # wait until we join the room

        self._stopped = False
        Console.flush()
        Console.flush_stderr()
        Console.get_console_stdout = lambda: \
                                     [] if sum(len(s) for s in self.current_input) == 0 \
                                        else [self.get_console_prompt(), *self.current_input]
        Console.get_console_stderr = lambda: []
        Console.get_console_cursor_offset = lambda: self.current_input_idx + self.console_prompt_len
        Console.get_console_prompt = lambda: '' if self._stopped else self.get_console_prompt()
        
        (self.maxcols, self.maxrows) = shutil.get_terminal_size()

        # loop over lines of input
        while not self.stop_event.is_set():
            self.console_prompt = self.get_console_prompt()
            msg = (
                '\033[0m' + # reset all modes
                '\r' + # go to beginning of line
                '\033[2K' + # erase line
                self.console_prompt
            )
            Console._write(msg)
            self.current_input.clear()
            self.current_input_idx = 0
            self.prev_input_idx = 0

            sys.stdout.flush()
            sys.stderr.flush()
            line = self._readline()
            if not line: continue

            # do something with finished line
            if line.lower() in ('\\q', '!q', '\\quit', '!quit'):
                self._stopped = True
                self.bot.shutdown()
                Console.flush()
                break
            if line:
                try:
                    self.bot.clear_response_event()
                    self.bot.send_bot_command(line)
                    self.bot.response_event.wait(self.cfg.response_timeout)
                    Console.flush()
                except Exception as ex:
                    log.error(ex, exc_info=True)
    
    def _readline(self) -> str:
        # loop over key presses during line input
        while not self.stop_event.is_set():
            try:
                ch = readkey()
                # uncomment this block for figuring out key codes in readchar_extended
                # if keycode.is_special(ch):
                #     # other control chars and combinations
                #     print(f"special ch={[c.encode().hex() for c in ch]} or ch={list(ch)} (name: {keycode.name(ch)})")
                # else:
                #     print(f"ch={[c.encode().hex() for c in ch]}")
                # continue
            except KeyboardInterrupt:
                self._stopped = True
                msg = (
                    '\033[0m' + # reset all modes
                    '\033[38;5;196m' + # bright red font
                    '^C\nGot keyboard interrupt, exiting (do not press interrupt again)...' +
                    '\033[0m' + # reset all modes
                    '\n'
                )
                sys.stderr.write(Console._format_text(msg))
                sys.stderr.flush()
                self.bot.shutdown()
                sys.stdout.flush()
                sys.stderr.flush()
                raise
            with Console.LockedWriter(source='c') as w:
                (self.maxcols, self.maxrows) = shutil.get_terminal_size()
                self.current_input_idx = max(0, min(self.current_input_idx, len(self.current_input)))

                if ch in (keycode.ENTER, keycode.CR, keycode.LF, keycode.ENTER_2):
                    self.history.append(self.current_input.copy())
                    if self.cfg.max_history_lines > 0 and len(self.history) > self.cfg.max_history_lines:
                        i = len(self.history) - self.cfg.max_history_lines
                        self.history = self.history[i:]
                    self.history_idx = len(self.history)
                    w.write('\n')
                    line = ''.join(self.current_input).strip()
                    self.current_input.clear()
                    self.current_input_idx = 0
                    return line

                elif ch == keycode.UP:
                    if self.history_idx > 0: # >0 ensures UP never clears the console
                        self.history_idx -= 1
                    self.replace_current_stdin_with_history(w, self.history_idx)

                elif ch == keycode.DOWN:
                    if self.history_idx <= len(self.history)-1:
                        self.history_idx += 1
                    self.replace_current_stdin_with_history(w, self.history_idx)

                # TODO: if SHIFT is held, should select while moving
                elif ch == keycode.LEFT:
                    self.move_cursor_left(1)
                    
                elif ch == keycode.RIGHT:
                    self.move_cursor_right(1)

                elif ch == keycode.CTRL_LEFT: # beginning of word
                    if self.current_input_idx <= 1: continue
                    # find index of first space, starting from the [current cursor pos]-1 and searching in reverse
                    # idx-1 to avoid no-op when prev char is ' '
                    n = len(self.current_input)
                    num_moves_left = next((idx for idx, c in enumerate(reversed(self.current_input[:self.current_input_idx-1]), 1) if c == ' '), self.current_input_idx)
                    if num_moves_left > 0:
                        self.move_cursor_left(num_moves_left)

                elif ch == keycode.CTRL_RIGHT: # beginning of next word
                    n = len(self.current_input)
                    if self.current_input_idx >= n-1: continue
                    # find index of first space, starting from the [current cursor pos]+1
                    # idx+1 to avoid no-op when next char is ' '
                    # start enumerate at 2 to jump to first char in next word
                    num_moves_right = next((idx for idx, c in enumerate(self.current_input[self.current_input_idx+1:], 2) if c == ' '), n-1 - self.current_input_idx)
                    if num_moves_right > 0:
                        self.move_cursor_right(num_moves_right)

                elif ch == keycode.HOME:
                    if self.current_input_idx > 0:
                        self.move_cursor_left(self.current_input_idx)
                        self.current_input_idx = 0

                elif ch == keycode.END:
                    n = len(self.current_input)
                    if self.current_input_idx < n:
                        self.move_cursor_right(n - self.current_input_idx)
                        self.current_input_idx = n
                        
                elif ch in keycode.BACKSPACE:
                    n = len(self.current_input)
                    if n > 0 and self.current_input_idx > 0:
                        nr = n - self.current_input_idx
                        
                        self.move_cursor_left(1)
                        if nr >= 0:
                            idx = self.current_input_idx
                            if nr > 0:
                                w.write(''.join(self.current_input[idx+1:]))
                            w.write(' ')
                            self.current_input_idx = n
                            self.move_cursor_left(nr + 1)

                            # update state
                            if idx < n:
                                del self.current_input[idx]
                            else:
                                del self.current_input[-1]

                elif ch in keycode.DELETE:
                    n = len(self.current_input)
                    nr = n - self.current_input_idx
                    if n > 0 and nr > 0:
                        # overwrite the rest of the line with chars after the 'deleted char'
                        w.write(''.join(self.current_input[self.current_input_idx+1:]))
                        w.write(' ') # the extra array index at the end of the line
                        idx = self.current_input_idx
                        # restore cursor pos
                        self.current_input_idx = n
                        self.move_cursor_left(nr)

                        # update state
                        if idx < n:
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

                elif ch in (keycode.ESC, keycode.ESC_2):
                    continue

                elif ch != keycode.SPACE and keycode.is_special(ch):
                    # other control chars and combinations
                    # if keycode.is_special(ch):
                    #    print(f"special ch={[c.encode().hex() for c in ch]} or ch={list(ch)} (name: {keycode.name(ch)})")
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
        return ""

# -------------------------------------------------------------
# dummy code to test the interactive console behavior

class DummyBot:
    def __init__(self, motd_event: Event, response_event: Event):
        self.motd_event = motd_event
        self.response_event = response_event
        motd_event.set()

    def clear_response_event(self):
        self.response_event.clear()

    def send_bot_command(self, msg):
        Console.writeln(f"DummyBot send_bot_command: '{msg}'")
        self.response_event.set()

    def send_message(self, channel: str, content: str):
        Console.writeln(f"DummyBot send_message: '{channel}' -> '{content}'")
        self.response_event.set()

    def send_pm(self, user: str, content: str):
        Console.writeln(f"DummyBot send_pm: '{user}' -> '{content}'")
        self.response_event.set()

    def send_raw(self, content: str):
        Console.writeln(f"DummyBot send_raw: '{content}'")
        self.response_event.set()

    def join_channel(self, channel: str):
        Console.writeln(f"DummyBot join_channel: '{channel}'")
        self.response_event.set()

    def close_room(self, warn=True):
        Console.writeln(f"DummyBot close_room. warn={warn}")
        self.response_event.set()

    def stop(self):
        Console.writeln(f"DummyBot stop")
        self.response_event.set()

    def shutdown(self):
        Console.writeln(f"DummyBot shutdown")
        self.response_event.set()

def test_interactive_console(stop_event: Event | None = None):
    cfg = parse_config()
    Console.enable_colors = cfg.enable_console_colors
    bot_response_event = Event()
    bot_motd_event = Event()
    bot = DummyBot(bot_motd_event, bot_response_event)
    iconsole = InteractiveConsole(bot, cfg, stop_event)
    iconsole.main_loop()

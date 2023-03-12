import os, sys, re, traceback
import logging
import multiprocessing
from multiprocessing.synchronize import Event
from readchar import readkey, key as keycode
import pyperclip # clipboard support
from typing import TextIO

from console import Console, log
from config import Config, parse_config
from osu_irc_bot import OsuIRCBot

def __get_console_prompt(insert_mode: bool = False):
    # https://stackoverflow.com/questions/4842424/list-of-ansi-color-escape-sequences
    # https://gist.github.com/fnky/458719343aabd01cfb17a3a4f7296797
    return (
        '\033[38;5;254;'+'48;5;238;'+'4m' + # dark gray bg, light fg, underline
        '>> console (!q to quit)' +
        ('*' if insert_mode else ' ') +
        '\033[0m' + # reset all modes
        ': '
    )

def interactive_console(bot: OsuIRCBot, cfg: Config, bot_motd_event: Event, bot_response_event: Event):
    insert_mode = False
    inputs_list = []
    inputs_idx = 0
    current_input = []
    current_input_idx = 0

    if bot_motd_event is not None:
        bot_motd_event.wait()
    Console.flush()
    Console.flush_stderr()

    # loop over lines of input
    while True:
        Console._write(__get_console_prompt(insert_mode))
        current_input = []
        current_input_idx = 0

        # loop over key presses during line input
        while True:
            sys.stdout.flush()
            sys.stderr.flush()
            ch = readkey()
            if ch in (keycode.ENTER, keycode.CR, keycode.LF):
                # current_input[::-1].index('')
                inputs_list.append(current_input)
                inputs_idx = len(inputs_list)
                Console._write('\n')
                break

            elif ch == keycode.UP:
                if inputs_idx > 0: # >0 ensures UP never clears the console
                    inputs_idx -= 1

                # erase current stdin
                if current_input_idx > 0:
                    Console._write('\b'*current_input_idx)
                if len(current_input) > 0:
                    Console._write(' '*len(current_input))
                    Console._write('\b'*len(current_input))
                    current_input = []
                    current_input_idx = 0
                
                if inputs_idx >= 0 and inputs_idx < len(inputs_list):
                    current_input = inputs_list[inputs_idx]
                    Console._write(''.join(current_input))
                    current_input_idx = len(current_input)

            elif ch == keycode.DOWN:
                if inputs_idx <= len(inputs_list)-1:
                    inputs_idx += 1

                # erase current stdin
                if current_input_idx > 0:
                    Console._write('\b'*current_input_idx)
                if len(current_input) > 0:
                    Console._write(' '*len(current_input))
                    Console._write('\b'*len(current_input))
                    current_input = []
                    current_input_idx = 0

                if inputs_idx >= 0 and inputs_idx < len(inputs_list):
                    current_input = inputs_list[inputs_idx]
                    Console._write(''.join(current_input))
                    current_input_idx = len(current_input)

            elif ch == keycode.LEFT:
                if current_input_idx > 0:
                    Console._write('\b')
                    current_input_idx -= 1

            elif ch == keycode.RIGHT:
                if current_input_idx < len(current_input):
                    Console._write(current_input[current_input_idx])
                    current_input_idx += 1

            elif ch == keycode.HOME:
                if current_input_idx > 0:
                    Console._write('\b'*current_input_idx)
                    current_input_idx = 0

            elif ch == keycode.END:
                if current_input_idx < len(current_input):
                    Console._write(''.join(current_input[current_input_idx:]))
                    current_input_idx = len(current_input)
                    
            elif ch in keycode.BACKSPACE:
                if len(current_input) > 0 and current_input_idx > 0:
                    n = len(current_input) - current_input_idx
                    
                    Console._write('\b')
                    Console._write(''.join(current_input[current_input_idx:]))
                    Console._write(' ')
                    Console._write('\b'*(n + 1))
                    if current_input_idx < len(current_input):
                        current_input = current_input[:current_input_idx-1] + current_input[current_input_idx:]
                    else:
                        current_input = current_input[:current_input_idx-1]
                    current_input_idx -= 1

            elif ch in keycode.DELETE:
                n = len(current_input) - current_input_idx
                if len(current_input) > 0 and n > 0:
                    # overwrite the rest of the line with chars after the 'deleted char'
                    Console._write(''.join(current_input[current_input_idx+1:]))
                    Console._write(' ') # the extra array index at the end of the line
                    Console._write('\b'*n) # move cursor back to original position
                    if len(current_input) > 0:
                        if current_input_idx < len(current_input):
                            current_input = current_input[:current_input_idx] + current_input[current_input_idx+1:]
                        else:
                            current_input = current_input[:current_input_idx]

            elif ch in keycode.INSERT:
                insert_mode = not insert_mode
                Console._write()
                Console._write('\r')
                Console._write(__get_console_prompt(insert_mode))
                Console._write(''.join(current_input[:current_input_idx]))

            elif ch == keycode.SPACE:
                if current_input_idx >= len(current_input):
                    Console._write(ch)
                    current_input.append(ch)
                    current_input_idx += 1
                elif insert_mode:
                    Console._write(ch)
                    current_input[current_input_idx] = ch
                    current_input_idx += 1
                else:
                    n = len(current_input) - current_input_idx
                    Console._write(ch)
                    Console._write(''.join(current_input[current_input_idx:]))
                    Console._write('\b'*n)
                    current_input.insert(current_input_idx, ch)
                    current_input_idx += 1

            elif ch == keycode.CTRL_V:
                text = pyperclip.paste()
                if current_input_idx >= len(current_input):
                    Console._write(text)
                    current_input_idx += len(text)
                    current_input.extend(list(text))
                elif not insert_mode:
                    Console._write(text)
                    rhs = current_input[current_input_idx:]
                    Console._write(''.join(rhs))
                    Console._write('\b'*len(rhs))
                    current_input = current_input[:current_input_idx] + list(text) + rhs
                    current_input_idx += len(text)
                else:
                    Console._write(text)
                    rhs = current_input[current_input_idx + len(text):]
                    current_input = current_input[:current_input_idx] + list(text) + rhs
                    current_input_idx += len(text)

            elif ch == keycode.TAB:
                # TODO: support tab-completion
                continue

            elif ch in (keycode.ESC):
                continue

            elif ch in keycode.__dict__.values():
                # other control chars and combinations
                continue

            else:
                # not a control character
                Console._write(ch)
                if insert_mode:
                    if current_input_idx < len(current_input):
                        current_input[current_input_idx] = ch
                    else:
                        current_input.append(ch)
                        current_input_idx = len(current_input) - 1
                else:
                    if current_input_idx < len(current_input) and len(current_input) > 0:
                        Console._write(''.join(current_input[current_input_idx:]))
                        Console._write('\b'*(len(current_input) - current_input_idx))
                    current_input.insert(current_input_idx, ch)
                current_input_idx += 1

        # do something with finished line
        line = ''.join(current_input).strip()
        if line.lower() in ('\\q', '!q', '\\quit', '!quit'):
            bot_response_event.clear()
            bot.close_room(warn=False)
            bot_response_event.wait(cfg.response_timeout)
            Console.flush()
            break
        if line:
            try:
                bot_response_event.clear()
                bot.send_bot_command(line)
                bot_response_event.wait(cfg.response_timeout)
                Console.flush()
            except Exception as ex:
                log.error(ex, exc_info=True)

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

def test_interactive_console():
    cfg = parse_config()
    Console.enable_colors = cfg.enable_console_colors
    bot_response_event = multiprocessing.Event()
    bot_motd_event = multiprocessing.Event()
    bot = DummyBot(bot_motd_event, bot_response_event)
    interactive_console(bot, cfg, bot_motd_event, bot_response_event)
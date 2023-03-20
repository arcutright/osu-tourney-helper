import sys
import traceback
import ssl
import threading
import multiprocessing
from multiprocessing.synchronize import Event
from typing import Callable
import irc
import irc.bot
import irc.client
import irc.connection
import jaraco.stream.buffer

from console import Console, log
from config import Config, parse_config, try_populate_map_info
from interactive_console import InteractiveConsole, test_interactive_console
from osu_irc_bot import OsuIRCBot

class IgnoreErrorsBuffer(jaraco.stream.buffer.DecodingLineBuffer):
    def handle_exception(self):
        pass

def populate_map_infos(cfg: Config, map_infos_populated_event: Event, stop_event: Event):
    map_infos_populated_event.clear()
    for map in cfg.maps:
        stop_event.wait(0.1) # avoid being rate-limited
        if stop_event.is_set():
            break
        try_populate_map_info(map)
    # in case we still got rate-limited, try again with some delay
    did_delay = False
    for map in cfg.maps:
        if stop_event.is_set():
            break
        if not map.map_info and map.mapid:
            if not did_delay:
                stop_event.wait(2)
                if stop_event.is_set():
                    break
                did_delay = True
            try_populate_map_info(map)
    map_infos_populated_event.set()

def trap_interrupt(fn: Callable, *args, **kwagrs):
    try:
        fn(*args, **kwagrs)
    except KeyboardInterrupt:
        pass

def main_bot():
    cfg = parse_config()
    Console.enable_colors = cfg.enable_console_colors

    # The LenientDecodingLineBuffer attempts UTF-8 but falls back to latin-1, which will avoid UnicodeDecodeError in all cases (but may produce unexpected behavior if an IRC user is using another encoding).
    # or use IgnoreErrorsBuffer to ignore all errors
    irc.client.ServerConnection.buffer_class = jaraco.stream.buffer.LenientDecodingLineBuffer

    if cfg.tls:
        connect_factory = irc.connection.Factory(wrapper=ssl.wrap_socket)
    else:
        connect_factory = irc.connection.Factory()

    stop_event = multiprocessing.Event()
    map_infos_populated_event = multiprocessing.Event()
    bot_response_event = multiprocessing.Event()
    bot_motd_event = multiprocessing.Event()
    bot = OsuIRCBot(
        cfg,
        response_event=bot_response_event,
        motd_event=bot_motd_event,
        map_infos_populated_event=map_infos_populated_event,
        connect_factory=connect_factory
    )
    iconsole = InteractiveConsole(bot, cfg, stop_event)

    map_info_thread = threading.Thread(target=trap_interrupt, args=(populate_map_infos, cfg, map_infos_populated_event, stop_event), daemon=True, name='map_info_fetch')
    console_thread = threading.Thread(target=trap_interrupt, args=(iconsole.main_loop, ), name='interactive_console')
    bot_thread = threading.Thread(target=bot.start, args=(), daemon=True, name='irc_bot')
    try:
        map_info_thread.start()
        console_thread.start()
        bot_thread.start()
        console_thread.join()
    except irc.client.ServerConnectionError as ex:
        traceback.print_exc()
    except Exception as ex:
        traceback.print_exc()
    finally:
        stop_event.set()
        bot.shutdown()
        bot_thread.join()
        map_info_thread.join()
        console_thread.join(2) # if it's hanging on readchar() it can't easily be killed

if __name__ == '__main__':
    try:
        Console.try_patch_stdin_stdout_behavior()
        # Console.test_colors()
        # test_interactive_console()
        main_bot()
    finally:
        Console.try_restore_stdin_stdout_behavior()
    sys.exit(0)

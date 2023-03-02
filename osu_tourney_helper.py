import sys
import traceback
import ssl
import logging
import time
import threading
import multiprocessing
from multiprocessing.synchronize import Event, Lock
import irc
import irc.bot
import irc.client
import irc.connection
import jaraco.logging
from jaraco.stream import buffer

from console import Console, log, redirect_log
from config import MapChoice, Config, parse_config, try_populate_map_info
from interactive_console import interactive_console, test_interactive_console
from osu_irc_bot import OsuIRCBot

class IgnoreErrorsBuffer(buffer.DecodingLineBuffer):
    def handle_exception(self):
        pass

def populate_map_infos(cfg: Config, map_infos_populated_event: Event, stop_event: Event):
    map_infos_populated_event.clear()
    for map in cfg.maps:
        if stop_event.is_set():
            break
        time.sleep(0.1) # avoid being rate-limited
        try_populate_map_info(map)
    # in case we still got rate-limited, try again with some delay
    did_delay = False
    for map in cfg.maps:
        if not map.map_info and map.mapid:
            if not did_delay:
                time.sleep(2)
                did_delay = True
            try_populate_map_info(map)
    map_infos_populated_event.set()

def main_bot():
    cfg = parse_config()
    jaraco.logging.setup(cfg)
    redirect_log(cfg.log_level)

    # The LenientDecodingLineBuffer attempts UTF-8 but falls back to latin-1, which will avoid UnicodeDecodeError in all cases (but may produce unexpected behavior if an IRC user is using another encoding).
    irc.client.ServerConnection.buffer_class = buffer.LenientDecodingLineBuffer

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
        bot_response_event=bot_response_event,
        bot_motd_event=bot_motd_event,
        map_infos_populated_event=map_infos_populated_event,
        connect_factory=connect_factory
    )

    map_info_thread = threading.Thread(target=populate_map_infos, args=(cfg, map_infos_populated_event, stop_event), daemon=True, name='map_info_fetch')
    console_thread = threading.Thread(target=interactive_console, args=(bot, cfg, bot_motd_event, bot_response_event), name='interactive_console')
    bot_thread = threading.Thread(target=bot.start, args=(), daemon=True, name='irc_bot')
    try:
        map_info_thread.start()
        console_thread.start()
        bot_thread.start()
        console_thread.join()
    except irc.client.ServerConnectionError as ex:
        traceback.print_exc()
        sys.exit(1)
    except Exception as ex:
        traceback.print_exc()
    finally:
        stop_event.set()
        bot.shutdown()
        bot_thread.join()
        map_info_thread.join()
        console_thread.join(2) # if it's hanging on readchar() it can't easily be killed
    sys.exit(0)

if __name__ == '__main__':
    # test_interactive_console()
    main_bot()

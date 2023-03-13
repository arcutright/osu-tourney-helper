import re
import threading
from typing import Union
import multiprocessing
from multiprocessing.synchronize import Event as MpEvent
import irc.bot
import irc.events
from irc.strings import lower as irc_lower
from irc.client import (
    ip_numstr_to_quad,
    ip_quad_to_numstr,
    Connection as IRCConnection,
    ServerConnection as IRCServerConnection,
    Event as IRCEvent
)

from config import Config
from console import Console, log

# for debugging / development, can be useful to log raw irc messages
log_unknown_irc_commands = True
known_irc_commands = set([
    'welcome',
    'motdstart', 'motd', 'motd2', 'endofmotd',
    'pubmsg', 'privmsg', 'dccmsg', 'dccchat',
    'connect', 'disconnect', 'die',
    'nicknameinuse',
    'ping', 'pong',
    'hello', 'join', 'nosuchchannel', # join events
    'currenttopic', 'topicinfo', # channel info when you join a channel
    'namreply', 'whoreply', 'whospcrpl', 'endofnames', # list of users when you join a channel
])

# osu irc commands
#  /join <#channel>    Join a channel
#  /part <#channel>    Leave a channel
#  /me <action>        Send an action message
#  /ignore <username>  Ignore a user (start hiding their messages)
#  /away <message>     Leave a message for everyone trying to contact you
#  /away               Clear the away message
#  /query <username>   Open a chat with username (replace spaces with underscores)

class NoReconnectStrategy(irc.bot.ReconnectStrategy):
    def run(self, bot):
        pass

class BaseOsuIRCBot(irc.bot.SingleServerIRCBot):
    """ Contains most of the general irc bot commands and handler logic,
    but does not contain any of the room management logic or define
    any of its own commands.
    """
    def __init__(self, cfg: Config,
                 bot_response_event: "Union[MpEvent, None]" = None,
                 bot_motd_event: "Union[MpEvent, None]" = None,
                 map_infos_populated_event: "Union[MpEvent, None]" = None,
                 **connect_params):
        server_list = [(cfg.server, cfg.port, cfg.password)] if cfg.password else [(cfg.server, cfg.port)]
        irc.bot.SingleServerIRCBot.__init__(self, server_list, nickname=cfg.nickname, realname=cfg.username, username=cfg.username, **connect_params)
        self.bot_target = cfg.bot_target or ''
        self.cfg = cfg

        self.bot_event_delay = cfg.event_delay_timeout or 0.8
        if bot_motd_event is not None: bot_motd_event.clear()
        if bot_response_event is not None: bot_response_event.clear()
        self.bot_response_event = bot_response_event if bot_response_event is not None else multiprocessing.Event()
        self.bot_motd_event = bot_motd_event if bot_motd_event is not None else multiprocessing.Event()
        self.map_infos_populated_event = map_infos_populated_event if map_infos_populated_event is not None else multiprocessing.Event()

        self.room_id = ''
        self._bot_motd_timer = None
        self._bot_response_timer = None
        self._did_motd_complete = False
        self._stopped = False

    ## ----------------------------------------------------------------------
    # startup / shutdown

    def start(self, timeout=0.2):
        """Start the bot."""
        self._bot_motd_timer = None
        self._bot_response_timer = None
        self._connect()
        try:
            while not self._stopped:
                self.reactor.process_once(timeout)
        except KeyboardInterrupt:
            self.stop()
            raise

    def stop(self):
        self._stopped = True
        self.disconnect()
        self._bot_motd_timer = None
        self._bot_response_timer = None
        
    def shutdown(self):
        self.recon = NoReconnectStrategy()
        self.stop()
        self.reactor.disconnect_all()

    def _on_disconnect(self, connection: IRCServerConnection, event: IRCEvent):
        try: self.close_room(warn=False)
        except Exception: pass
        if self._stopped:
            return
        return super()._on_disconnect(connection, event)

    ## ----------------------------------------------------------------------
    #  public send() functions

    def join_channel(self, channel: str, password=''):
        if not channel: return
        channel = self._format_channel(channel)
        self.connection.join(channel, password)
        # responds with 'join', 'currenttopic', 'topicinfo'

    def send_message(self, channel: str, content: str):
        """Send a message to a channel on the server"""
        self._clear_response_event()
        channel = self._format_channel(channel)
        self.connection.privmsg(channel, content)

    def send_pm(self, user: str, content: str):
        """Send a private message to a user on the server"""
        self._clear_response_event()
        user = self._format_user(user)
        self.connection.privmsg(user, content)

    def send_raw(self, content: str):
        """Send a raw string to the server (will be padded with CLRF for you)"""
        self._clear_response_event()
        self.connection.send_raw(content)

    ## ----------------------------------------------------------------------
    # helpers

    def get_user(self, userstr: str):
        """Grab user name from either 'user' or 'user!cho@ppy.sh'"""
        i = userstr.rfind('!')  # example: ':Nhato!cho@ppy.sh QUIT :quit', source='Nhato!cho@ppy.sh'
        return userstr[:i] if i > 0 else userstr
    
    def refers_to_self(self, name: str):
        """Check if name refers to this bot's irc name/nickname (this calls get_user(name), it can handle 'user!cho@ppy.sh')"""
        ircname = irc_lower(self.connection.ircname)
        nickname = irc_lower(self.connection.get_nickname())
        return (irc_lower(name) in (ircname, nickname)
                or irc_lower(self.get_user(name)) in (ircname, nickname))
    
    def refers_to_bot_target(self, name: str):
        """Check if name refers to bot_target (BanchoBot) irc name/nickname (this calls get_user(name), it can handle 'user!cho@ppy.sh')"""
        ircname = irc_lower(self.bot_target)
        return (irc_lower(name) == ircname
                or irc_lower(self.get_user(name)) == ircname)
    
    def _clear_response_event(self):
        if self._bot_response_timer is not None:
            self._bot_response_timer.cancel()
        self.bot_response_event.clear()

    def _format_channel(self, channel: str):
        if not channel: return ''
        return '#' + channel.strip().strip('#').strip().replace(' ', '_')
    
    def _format_user(self, user: str):
        user = self.get_user(user)
        if not user: return ''
        return user.strip().strip('#').strip().replace(' ', '_')
    
    ## ----------------------------------------------------------------------
    #  join / welcome / message of the day handling

    def on_welcome(self, conn: IRCServerConnection, event: IRCEvent):
        messages = "', '".join([str(arg) for arg in event.arguments])
        log.info(f"Connected to '{event.source}'. Messages: ['{messages}']")

        if self._bot_motd_timer is not None:
            self._bot_motd_timer.cancel()
        self._bot_motd_timer = threading.Timer(self.cfg.motd_timeout, lambda: self._motd_complete())
        self._bot_motd_timer.start()
    
    def on_join(self, conn: IRCServerConnection, event: IRCEvent):
        user = self.get_user(event.source)
        log.info(f"'{user}' joined '{event.target}'")

    def on_motd(self, conn: IRCServerConnection, event: IRCEvent):
        if not self.refers_to_self(event.target): return
        Console.writeln(' '.join(event.arguments))

        # set the motd event flag a bit after the motd stops coming in
        if self._bot_motd_timer is not None:
            self._bot_motd_timer.cancel()
        self._bot_motd_timer = threading.Timer(self.bot_event_delay, lambda: self._motd_complete())
        self._bot_motd_timer.start()

    def on_motd2(self, conn: IRCServerConnection, event: IRCEvent):
        self.on_motd(conn, event)

    def on_motdstart(self, conn: IRCServerConnection, event: IRCEvent):
        self.on_motd(conn, event)
    
    def on_endofmotd(self, conn: IRCServerConnection, event: IRCEvent):
        if not self.refers_to_self(event.target): return
        Console.writeln(' '.join(event.arguments))
        self._motd_complete()

    def _motd_complete(self):
        if self._did_motd_complete: return
        self._did_motd_complete = True

        if self._bot_motd_timer is not None:
            self._bot_motd_timer.cancel()
        self.bot_motd_event.set()
        self._bot_motd_timer = None

    ## ----------------------------------------------------------------------
    # leave / error events

    def on_part(self, conn: IRCServerConnection, event: IRCEvent):
        self._on_part_with_reason(conn, event, 'part')

    def on_kick(self, conn: IRCServerConnection, event: IRCEvent):
        self._on_part_with_reason(conn, event, 'kick')
        
    def on_error(self, conn: IRCServerConnection, event: IRCEvent):
        if self.refers_to_self(event.source):
            log.error(f"onerror, user: '{event.source}' target: '{event.target}', args: '{' '.join(event.arguments)}'")
    
    def _on_part_with_reason(self, conn: IRCServerConnection, event: IRCEvent, reason: str):
        user = self.get_user(event.source)
        log.info(f"'{user}' left channel '{event.target}', reason: {reason}, args: '{' '.join(event.arguments)}'")

    ## ----------------------------------------------------------------------
    # other irc events

    def on_nicknameinuse(self, conn: IRCServerConnection, event: IRCEvent):
        conn.nick(conn.get_nickname() + "_")

    def on_nickcollision(self, conn: IRCServerConnection, event: IRCEvent):
        self.on_nicknameinuse(conn, event)

    def on_nosuchchannel(self, conn: IRCServerConnection, event: IRCEvent):
        if not self.refers_to_self(event.target): return
        messages = "', '".join([str(arg) for arg in event.arguments])
        log.warn(f"No such channel! messages: ['{messages}']\n")

    def on_currenttopic(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, f"currenttopic src:'{event.source}', target:'{event.target}', args:'{' '.join(event.arguments)}', tags:'{' '.join(event.tags)}'")

    def on_topicinfo(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, f"topicinfo src:'{event.source}', target:'{event.target}', args:'{' '.join(event.arguments)}', tags:'{' '.join(event.tags)}'")
        
    def on_endofnames(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(IRCEvent('stats', '', ''), ' '.join(event.arguments))
    
    def on_namreply(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, f"namreply src:'{event.source}', target:'{event.target}', args:'{' '.join(event.arguments)}', tags:'{' '.join(event.tags)}'")

    ## ----------------------------------------------------------------------
    # raw message handling

    def on_privnotice(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, ' '.join(event.arguments))

    def on_pubmsg(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, ' '.join(event.arguments))
    
    def on_pubnotice(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, ' '.join(event.arguments))

    #def on_dccmsg(self, conn: IRCConnection, event: IRCEvent):
    #    # non-chat DCC messages are raw bytes; decode as text
    #    text = event.arguments[0].decode('utf-8')
    #    conn.privmsg("You said: " + text)

    #def on_dccchat(self, conn: IRCConnection, event: IRCEvent):
    #    if len(event.arguments) != 2:
    #        return
    #    args = event.arguments[1].split()
    #    if len(args) == 4:
    #        try:
    #            address = ip_numstr_to_quad(args[2])
    #            port = int(args[3])
    #        except ValueError:
    #            return
    #        self.dcc_connect(address, port)

    def on_all_raw_messages(self, connection: IRCServerConnection, event: IRCEvent):
        if not log_unknown_irc_commands: return
        if not event or not event.arguments: return
        if event.source:
            if event.source not in (connection.ircname, connection.server, connection.real_server_name):
                source = irc_lower(event.source)
                if source not in (
                    irc_lower(connection.ircname), irc_lower(connection.get_nickname()),
                    irc_lower(connection.server), irc_lower(connection.real_server_name),
                    irc_lower(self.bot_target)
                ):
                    return
        line = str(event.arguments[0])
        msgs = line.split(':')
        if len(msgs) < 2: return

        # examples
        # :cho.ppy.sh 001 mastaa_p :Welcome to the osu!Bancho.
        # :cho.ppy.sh 375 mastaa_p :-
        # :cho.ppy.sh 372 mastaa_p :- boat:   https://twitter.com/banchoboat
        # :cho.ppy.sh 403 mastaa_p #BanchoBot :No such channel #BanchoBot
        # :Slotki_Levi!cho@ppy.sh QUIT :replaced (Normal fced0f89-659a-497c-b96c-c21d872abe64)
        # :Nhato!cho@ppy.sh QUIT :quit
        # :Qwertie_!cho@ppy.sh QUIT :ping timeout 180s
        commands = msgs[1].split(' ')
        source = commands[0]
        i = source.rfind('!')
        if i != -1:
            source = source[:i]

        command = commands[1].lower()
        command = irc.events.numeric.get(command, command)

        target = irc_lower(commands[2]) if len(commands) >= 3 else ''

        # content = msgs[2].strip() if len(msgs) >= 3 else ''

        if self.refers_to_self(target) or self.refers_to_self(source):
            if command not in known_irc_commands:
                Console.writeln(f"raw: '{line}'")
                #Console.writeln(f"source: {source}, command: {command}: content: [{content}]")

    ## ----------------------------------------------------------------------
    #  dispatcher for commands
    #  this handles triggering response_event

    def do_command(self, event: IRCEvent, msg: str):
        nick = event.source
        conn = self.connection
        cmd = event.type

        if cmd == "disconnect":
            Console.writeln(f"discconect by request: {msg}")
            self.disconnect()
        elif cmd == "die":
            self.die()
        elif cmd in ("motd", "currenttopic", "topicinfo"):
            Console.writeln(f"{msg}")
        elif cmd in ("privmsg", "pubmsg"):
            Console.writeln(f"{msg}")
        elif cmd in ("namreply", "whoreply"):
            Console.writeln(f"{msg}")
        elif cmd in ("privnotice", "pubnotice"):
            Console.writeln(f"{cmd}: {msg}")
        elif cmd == "stats":
            for chname, chobj in self.channels.items():
                conn.notice(nick, "--- Channel statistics ---")
                conn.notice(nick, "Channel: " + chname)
                users = sorted(chobj.users())
                conn.notice(nick, "Users: " + ", ".join(users))
                opers = sorted(chobj.opers())
                conn.notice(nick, "Opers: " + ", ".join(opers))
                voiced = sorted(chobj.voiced())
                conn.notice(nick, "Voiced: " + ", ".join(voiced))
            self.bot_response_event.set() # not waiting on an IRC response
        elif cmd == "dcc":
            dcc = self.dcc_listen()
            conn.ctcp("DCC", nick,
                f"CHAT chat {ip_quad_to_numstr(dcc.localaddress)} {dcc.localport}"
            )
        else:
            log.warn(f"unrecognized cmd: '{cmd}', msg: '{msg}'\n")
            # conn.notice(nick, f"Not understood: {cmd}")

        # set the event flag a bit after the response stops coming in
        if self._bot_response_timer is not None:
            self._bot_response_timer.cancel()
        self._bot_response_timer = threading.Timer(self.bot_event_delay, lambda: self.bot_response_event.set())
        self._bot_response_timer.start()

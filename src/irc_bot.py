from __future__ import annotations
import sys
import re
import threading
from typing import Callable
import multiprocessing
from multiprocessing.synchronize import Event as MpEvent
import irc.bot
import irc.events
import irc.dict
from irc.strings import lower as irc_lower
from irc.client import (
    ip_numstr_to_quad,
    ip_quad_to_numstr,
    Connection as IRCConnection,
    ServerConnection as IRCServerConnection,
    Event as IRCEvent,
    MessageTooLong
)

from helpers import value_or_fallback
from text import plaintext
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
                 response_event: MpEvent | None = None,
                 motd_event: MpEvent | None = None,
                 map_infos_populated_event: MpEvent | None = None,
                 **connect_params):
        server_list = [(cfg.server, cfg.port, cfg.password)] if cfg.password else [(cfg.server, cfg.port)]
        irc.bot.SingleServerIRCBot.__init__(self, server_list, nickname=cfg.nickname, realname=cfg.username, username=cfg.username, **connect_params)
        self.bot_target = value_or_fallback(cfg.bot_target, '')
        self.cfg = cfg

        self.event_delay_timeout = value_or_fallback(cfg.event_delay_timeout, 0.8)
        self.motd_timeout = value_or_fallback(cfg.motd_timeout, 3.0)
        self.response_timeout = value_or_fallback(cfg.response_timeout, 5.0)
        self.response_event = value_or_fallback(response_event, multiprocessing.Event())
        self.motd_event = value_or_fallback(motd_event, multiprocessing.Event())
        self.motd_event.clear()
        self.response_event.clear()
        self.map_infos_populated_event = value_or_fallback(map_infos_populated_event, multiprocessing.Event())

        self.room_id = ''
        self._motd_timer = None
        self._response_timer = None
        self._did_motd_complete = False
        self._stopped = False

    ## ----------------------------------------------------------------------
    # startup / shutdown

    def start(self, timeout=0.2):
        """Start the bot."""
        self.clear_motd_event()
        self.clear_response_event()
        self._connect()
        if self.connection.is_connected():
            self.set_motd_event()
        try:
            while not self._stopped:
                self.reactor.process_once(timeout)
        except KeyboardInterrupt:
            self.stop()
            raise

    def stop(self):
        self._stopped = True
        self.disconnect(msg="Goodbye")
        
    def shutdown(self):
        self.recon = NoReconnectStrategy()
        self.stop()
        self.reactor.disconnect_all()

    def _on_disconnect(self, connection: IRCServerConnection, event: IRCEvent):
        self.channels = irc.dict.IRCDict()
        if not self._stopped:
            self.recon.run(self)

    ## ----------------------------------------------------------------------
    #  public send() functions

    def join_channel(self, channel: str, password=''):
        if not channel: return
        channel = self._format_channel(channel)
        self.connection.join(channel, password)
        # responds with 'join', 'currenttopic', 'topicinfo'

    def send_message(self, channel: str, content: str):
        """Send a message to a channel on the server"""
        self.clear_response_event()
        channel = self._format_channel(channel)
        sent = self.__try_send(content, lambda msg: self.connection.privmsg(channel, msg))
        if sent: Console.writeln(f"self->{channel}: {sent}", fg='gray')

    def send_pm(self, user: str, content: str):
        """Send a private message to a user on the server"""
        self.clear_response_event()
        user = self._format_user(user)
        sent = self.__try_send(content, lambda msg: self.connection.privmsg(user, msg))
        if sent: Console.writeln(f"self->{user}: {sent}", fg='gray')

    def send_raw(self, content: str):
        """Send a raw string to the server (will be padded with CLRF for you)"""
        self.clear_response_event()
        sent = self.__try_send(content, lambda msg: self.connection.send_raw(msg))
        if sent: Console.writeln(f"self (raw): {sent}", fg='gray')

    def __try_send(self, content: str, send_func: Callable[[str]]):
        try:
            send_func(content)
            return content
        except MessageTooLong:
            pass
        try:
            content2 = plaintext(content, remove_links=False)
            if len(content2) >= len(content):
                raise MessageTooLong()
            log.info(f"Failed to send: message too long (max 512 bytes per irc message). Trying plaintext conversion...")
            send_func(content2)
            return content2
        except Exception:
            pass
        try:
            content3 = plaintext(content2, remove_links=True)
            if len(content3) >= len(content2):
                raise MessageTooLong()
            log.info(f"Failed to send: message too long (max 512 bytes per irc message). Removing links...")
            send_func(content3)
            return content3
        except Exception:
            log.warn(f"Failed to send: message too long (max 512 bytes per irc message). \nContent: '{content}'")
            return None

    ## ----------------------------------------------------------------------
    # helpers

    def get_user(self, userstr: str):
        """Grab user name from either 'user' or 'user!cho@ppy.sh'"""
        if not userstr: return ''
        i = userstr.rfind('!')  # example: ':Nhato!cho@ppy.sh QUIT :quit', source='Nhato!cho@ppy.sh'
        return userstr[:i] if i > 0 else userstr
    
    def refers_to_self(self, name: str):
        """Check if name refers to this bot's irc name/nickname (this calls get_user(name), it can handle 'user!cho@ppy.sh')"""
        if not name: return False
        ircname = irc_lower(self.connection.ircname)
        nickname = irc_lower(self.connection.get_nickname())
        name = irc_lower(self.get_user(name))
        return name in (ircname, nickname)
    
    def refers_to_server(self, name: str):
        """Check if name refers to bot_target (BanchoBot) irc name/nickname or the server itself (this calls get_user(name), it can handle 'user!cho@ppy.sh')"""
        if not name: return False
        ircname = irc_lower(self.bot_target)
        name = irc_lower(self.get_user(name))
        return (name == ircname
                or name == irc_lower(self.connection.get_server_name()))
    
    def _format_channel(self, channel: str):
        if not channel: return ''
        return '#' + channel.strip().strip('#').strip().replace(' ', '_')
    
    def _format_user(self, user: str):
        user = self.get_user(user)
        if not user: return ''
        return user.strip().strip('#').strip().replace(' ', '_')
    
    ## ----------------------------------------------------------------------
    #  irc response / motd events

    def _on_response_complete(self):
        """Callback to set `response_event`
        (usually after some delay `event_delay_timeout`, when there has been no further response)
        """
        self.response_event.set()
        self.cancel_response_event()

    def __on_motd_complete(self):
        if self._did_motd_complete: return
        self._on_motd_complete()
        self._did_motd_complete = True

    def _on_motd_complete(self):
        """Callback to set `motd_event` and detach any running timers when motd is complete
        or `motd_timeout` has been exceeded. \n
        This is only fired if `_did_motd_complete` was not already set
        """
        self.motd_event.set()
        self._did_motd_complete = True
        self.cancel_motd_event()

    def cancel_response_event(self):
        """Cancel previous bot response events but does not affect the state of `response_event`"""
        if self._response_timer is not None:
            self._response_timer.cancel()
            self._response_timer = None

    def cancel_motd_event(self):
        """Cancel previous bot motd events but does not affect the state of `motd_event`"""
        if self._motd_timer is not None:
            self._motd_timer.cancel()
            self._motd_timer = None

    def clear_response_event(self):
        """Cancel previous response events and clear `response_event`"""
        self.cancel_response_event()
        self.response_event.clear()

    def clear_motd_event(self):
        """Cancel previous motd events and clear `motd_event` and the `_did_motd_complete` flag"""
        self.cancel_motd_event()
        self.motd_event.clear()
        self._did_motd_complete = False

    def set_response_event(self, delay: float | None = None):
        """ Sets `response_event` after a delay (if None, defaults to `event_delay_timeout`)"""
        self.cancel_response_event()
        if delay is None:
            delay = self.event_delay_timeout
        if delay == 0:
            self._on_response_complete()
        else:
            self._response_timer = threading.Timer(delay, self._on_response_complete)
            self._response_timer.start()

    def set_motd_event(self, delay: float | None = None):
        """ Sets `motd_event` after a delay (if None, defaults to `motd_timeout`). \n
        Note that motd event will only ever be set once (see `_did_motd_complete`)
        """
        if self._did_motd_complete: return
        self.cancel_motd_event()
        if delay is None:
            delay = self.motd_timeout
        if delay == 0:
            self.__on_motd_complete()
        else:
            self._motd_timer = threading.Timer(delay, self.__on_motd_complete)
            self._motd_timer.start()
    
    ## ----------------------------------------------------------------------
    #  join / welcome

    def on_welcome(self, conn: IRCServerConnection, event: IRCEvent):
        messages = "', '".join([str(arg) for arg in event.arguments])
        log.info(f"Connected to '{event.source}'. Messages: ['{messages}']")
        self.set_motd_event()
    
    def on_join(self, conn: IRCServerConnection, event: IRCEvent):
        user = self.get_user(event.source)
        log.info(f"'{user}' joined '{event.target}'")

    ## ----------------------------------------------------------------------
    #  message of the day handling

    def on_motd(self, conn: IRCServerConnection, event: IRCEvent):
        if not self.refers_to_self(event.target): return
        Console.writeln(' '.join(event.arguments))
        self.set_motd_event()

    def on_motd2(self, conn: IRCServerConnection, event: IRCEvent):
        self.on_motd(conn, event)

    def on_motdstart(self, conn: IRCServerConnection, event: IRCEvent):
        self.on_motd(conn, event)
    
    def on_endofmotd(self, conn: IRCServerConnection, event: IRCEvent):
        if not self.refers_to_self(event.target): return
        Console.writeln(' '.join(event.arguments))
        self.set_motd_event(delay=0)

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
        log.warn(f"No such channel! message: '{' '.join(event.arguments)}'\n")

    def on_currenttopic(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, ' '.join(event.arguments))

    def on_topicinfo(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, ' '.join(event.arguments))
        
    def on_endofnames(self, conn: IRCServerConnection, event: IRCEvent):
        # Console.writeln(f"endofnames src:'{event.source}', target:'{event.target}', args:'{' '.join(event.arguments)}', tags:'{' '.join(event.tags)}'")
        self.do_command(IRCEvent('stats', '', ''), ' '.join(event.arguments))
    
    def on_namreply(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, ' '.join(event.arguments))

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
        msg2 = f"src:'{event.source}', target:'{event.target}', args:'{' '.join(event.arguments)}', tags:'{' '.join(event.tags)}', msg:'{msg}'"

        if cmd == "disconnect":
            Console.writeln(f"disconnect by request: {msg2}")
            self.disconnect()
        elif cmd == "die":
            self.shutdown()
            sys.exit(-92)
        elif cmd in ("motd", "motd2", "motdstart", "motdend"):
            Console.writeln(msg)
        elif cmd in ("currenttopic", "topicinfo"):
            # Console.writeln(f"{cmd} {msg}")
            pass
        elif cmd in ("privmsg", "pubmsg"):
            Console.writeln(msg)
        elif cmd in ("namreply", "whoreply"):
            # Console.writeln(f"{cmd} {msg2}")
            pass
        elif cmd in ("privnotice", "pubnotice"):
            Console.writeln(f"{cmd}: {msg2}")
        elif cmd == "stats":
            chname: str
            chobj: irc.bot.Channel
            for (chname, chobj) in self.channels.items():
                # notes from https://osu.ppy.sh/wiki/en/Community/Internet_Relay_Chat
                # voiced or +user prefix: joined via IRC
                # opers or @user prefix: global chat mod, GMT, etc.
                Console.writeln("---------- !stats ----------")
                Console.writeln(f"Channel: {chname}")
                users = sorted(chobj.users())
                Console.writeln(f"Users: {', '.join(users)}")
                opers = sorted(chobj.opers())
                Console.writeln(f"Opers: {', '.join(opers)}")
                voiced = sorted(chobj.voiced())
                Console.writeln(f"Voiced: {', '.join(voiced)}")
                Console.writeln("----------------------------")
        elif cmd == "dcc":
            dcc = self.dcc_listen()
            conn.ctcp("DCC", nick,
                f"CHAT chat {ip_quad_to_numstr(dcc.localaddress)} {dcc.localport}"
            )
        else:
            log.warn(f"unrecognized cmd: '{cmd}', msg2: '{msg2}'\n")
            # conn.notice(nick, f"Not understood: {cmd}")

        # set the event flag a bit after the response stops coming in
        self.set_response_event()

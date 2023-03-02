import re
import threading
import multiprocessing
from multiprocessing.synchronize import Event as MpEvent, Lock as MpLock
import pprint
from more_itertools import consume, always_iterable, repeatfunc
import irc
from irc.strings import lower as irc_lower
import irc.bot
import irc.client
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

class OsuIRCBot(irc.bot.SingleServerIRCBot):
    def __init__(self, cfg: Config,
                 bot_response_event: MpEvent = None,
                 bot_motd_event: MpEvent = None,
                 map_infos_populated_event: MpEvent = None,
                 bot_event_delay: float = 0.8,
                 **connect_params):
        server_list = [(cfg.server, cfg.port, cfg.password)] if cfg.password else [(cfg.server, cfg.port)]
        irc.bot.SingleServerIRCBot.__init__(self, server_list, nickname=cfg.nickname, realname=cfg.username, username=cfg.username, **connect_params)
        self.channel = cfg.initial_channel or ''
        self.channel_password = cfg.initial_channel_password or ''
        self.bot_target = cfg.bot_target or ''
        self.cfg = cfg
        self.raw_commands = cfg.raw_commands or []

        self.bot_event_delay = bot_event_delay or 0.8
        if bot_motd_event is not None: bot_motd_event.clear()
        if bot_response_event is not None: bot_response_event.clear()
        self.bot_response_event = bot_response_event if bot_response_event is not None else multiprocessing.Event()
        self.bot_motd_event = bot_motd_event if bot_motd_event is not None else multiprocessing.Event()
        self.map_infos_populated_event = map_infos_populated_event if map_infos_populated_event is not None else multiprocessing.Event()

        self.room_id = ''
        self.__bot_motd_timer = None
        self.__bot_response_timer = None
        self.__did_motd_complete = False
        self.__stopped = False
        self.__creating_room = False
        self.__closing_room = False

    ## ----------------------------------------------------------------------
    # startup / shutdown

    def start(self, timeout=0.2):
        """Start the bot."""
        self.__bot_motd_timer = None
        self.__bot_response_timer = None
        self._connect()
        while not self.__stopped:
            self.reactor.process_once(timeout)

    def stop(self):
        self.__stopped = True
        try: self.close_room(warn=False)
        except Exception: pass
        self.disconnect()
        self.__bot_motd_timer = None
        self.__bot_response_timer = None
        
    def shutdown(self):
        self.recon = NoReconnectStrategy()
        self.stop()
        self.reactor.disconnect_all()

    def _on_disconnect(self, connection: IRCServerConnection, event: IRCEvent):
        try: self.close_room(warn=False)
        except Exception: pass
        if self.__stopped:
            return
        return super()._on_disconnect(connection, event)
    
    ## ----------------------------------------------------------------------

    def __init_room(self, room_id=''):
        room_id = room_id or self.room_id
        if not self.room_id:
            log.error(f"room '{self.cfg.room_name}' is not open!")
            return
        room_id = self.__format_channel(room_id)
        self.send_message(room_id, f'!mp password {self.cfg.room_password}')
        self.send_message(room_id, f'!mp set {self.cfg.teammode} {self.cfg.scoremode}')
        self.send_message(room_id, f'!mp map 2538074') # demetori - casket of star
        self.send_message(room_id, f'!mp mods freemod')
        self.send_message(room_id, f'!mp settings') # show current room settings
        if self.raw_commands:
            for cmd in self.raw_commands:
                self.send_message(room_id, cmd)
            self.raw_commands = []

    def create_room(self, room_name=''):
        room_name = room_name or self.cfg.room_name
        if not room_name:
            log.error(f"room_name not configured!")
            return
        if self.room_id:
            log.error(f"room '{room_name}' is already open! id = '{self.room_id}'")
            return
        # log.info(f"make room: {room_name}")
        self._clear_response_event()
        self.__creating_room = True
        self.room_id = ''
        self.cfg.room_name = room_name
        self.send_pm(self.bot_target, f"!mp make {room_name}")
    
    def close_room(self, warn=True):
        try:
            room_id = self.room_id
            if not room_id:
                if warn:
                    log.error(f"room '{self.cfg.room_name}' is not open!")
                return
            # log.info(f"close room: {self.cfg.room_name}")
            self.__closing_room = True
            self.send_message(room_id, f'!mp close')
        except Exception as ex:
            if warn:
                log.error(ex, exc_info=True)

    def invite_participants(self):
        if not self.room_id: 
            log.error(f"room was not created!")
            return
        for player in self.cfg.players:
            self.send_message(self.room_id, f"!mp invite {player}")
        for admin in self.cfg.admins:
            self.send_message(self.room_id, f"!mp invite {admin}")
        self.send_message(self.room_id, f"!mp addref {' '.join(self.cfg.admins)}") # may need them to be in the room first...

    def lookup_map(self, label: str):
        return next((m for m in self.cfg.maps if m.label == label), None)

    def choose_map(self, label: str):
        map = self.lookup_map(label)
        log.info(f"choose map: {map}")
        if map is None:
            log.error(f"map is empty! could not find map '{label}'")
            return
        self.send_message(self.room_id, f'!mp map {map.mapid}')

    def join_channel(self, channel: str, password=''):
        if not channel: return
        channel = self.__format_channel(channel)
        self.connection.join(channel, password)
        # responds with 'join', 'currenttopic', 'topicinfo'
    
    def send_bot_command(self, content: str):
        """Sends a command or message to either the bot_target or to the current channel.
        By default, most things go to the channel if we are connected to one.
        
        Exceptions:
        - anything that starts with '/' (eg, '/join <room>' will go to bot_target)
        - '!mp make' commands go to bot_target
        
        This also has some special commands (try !help), such as '!mp map <label>', '!mp invite_all'
        """
        content = content.strip()
        command = content.lower()
        map = None
        is_map_request = False

        if command.startswith('help') or command.startswith('!mp help') or command.startswith('!help'):
            Console.writeln('--- extra commands, not in bancho ---')
            Console.writeln('!stats             see joined channel stats')
            Console.writeln('!debug, !config    see the current config in the .ini file + joined channel stats')
            Console.writeln('!mp invite_all     invite all players and admins configured in the .ini file')
            Console.writeln('!mp map <label>    choose map based on label configured in the .ini file (eg: !mp map hd1)')
            Console.writeln('!mp map_list       list all maps currently configured')
            Console.writeln('--- bancho help ---')
            self.send_pm(self.cfg.bot_target, content)
            return
        
        elif command.startswith('!stats') or command.startswith('!debug') or command.startswith('!config'):
            if command.startswith('!debug') or command.startswith('!config'):
                Console.writeln('--- config ---')
                pp = pprint.PrettyPrinter(indent=2)
                Console.writeln(pp.pformat(self.cfg))
                Console.writeln('--- !stats ---')
            self.do_command(IRCEvent('stats', '', ''), '')
            return
        
        elif command.startswith('!mp maplist') or command.startswith('!mp map_list'):
            self.map_infos_populated_event.wait(1)
            if not self.map_infos_populated_event.is_set():
                Console.writeln("Waiting (max 30s) for map infos to be populated (this is a one-time cost)")
                self.map_infos_populated_event.wait(30)
            Console.writeln(f"{'label':<8}  {'mapid':<9}  {'mods':<12}  {'map link':<38}  {'map description'}")
            Console.writeln(f"{'-'*8}  {'-'*9}  {'-'*12}  {'-'*38} {'-'*20}")
            for map in self.cfg.maps:
                Console.writeln(f"{map.label:<8}  {map.mapid:>9}  {map.mods:<12}  {map.get_osu_link():<38}  {map.description}")
            Console.writeln("short")
            self.bot_response_event.set()
            return
        
        elif command.startswith('!mp inviteall') or command.startswith('!mp invite_all'):
            self.invite_participants()
            return
        
        elif command.startswith('!mp map'):
            aftermap = content[content.index('map')+4:]
            # two valid cases here. The wacky logic is to handle if map label has spaces and ends with a number
            # 1. !mp map <mapid> [<playmode>]
            # 2. !mp map <map label> [<playmode>]
            mapid = aftermap.strip().upper()
            rhs = ''
            map = self.lookup_map(mapid)
            if map is None:
                i = aftermap.rfind(' ')
                if i != -1:
                    mapid = aftermap[:i].upper().strip()
                    rhs = aftermap[i:]
                    map = self.lookup_map(mapid)
            if map is not None:
                mapid = str(map.mapid)
            content = f'!mp map {mapid}{rhs}'
            is_map_request = True

        if self.room_id and not command.startswith('!mp make') and not command.startswith('/'):
            self.send_message(self.room_id, content)
        else:
            self.send_pm(self.cfg.bot_target, content)

        if is_map_request and self.room_id and map:
            self.send_message(self.room_id, f'!mp set {self.cfg.teammode} {self.cfg.scoremode}')
            self.send_message(self.room_id, f'!mp mods {map.mods}')
            mirrors = map.get_mirror_links()
            if mirrors:
                self.send_message(self.room_id, "------ mirror links -------")
                self.send_message(self.room_id, ', '.join(mirrors))

        # no response expected for plain messages to a room
        if not command.startswith('!') and not command.startswith('/'):
            self.bot_response_event.set()

    ## ----------------------------------------------------------------------
    #  join / welcome / message of the day handling

    def __motd_complete(self):
        if self.__did_motd_complete: return
        self.__did_motd_complete = True

        if self.__bot_motd_timer is not None:
            self.__bot_motd_timer.cancel()
        self.bot_motd_event.set()
        self.__bot_motd_timer = None

        if self.cfg.room_name:
            self.create_room()
        elif self.channel:
            self.join_channel(self.channel, self.channel_password)
        elif self.raw_commands:
            for cmd in self.raw_commands:
                self.send_bot_command(cmd)
            self.raw_commands = []

    def on_welcome(self, conn: IRCServerConnection, event: IRCEvent):
        messages = "', '".join([str(arg) for arg in event.arguments])
        log.info(f"Connected to '{event.source}'. Messages: ['{messages}']")

        if self.__bot_motd_timer is not None:
            self.__bot_motd_timer.cancel()
        self.__bot_motd_timer = threading.Timer(self.cfg.motd_timeout, lambda: self.__motd_complete())
        self.__bot_motd_timer.start()

    def get_user(self, userstr: str):
        """Grab user name from either 'user' or 'user!cho@ppy.sh'"""
        i = userstr.rfind('!')  # example: ':Nhato!cho@ppy.sh QUIT :quit', source='Nhato!cho@ppy.sh'
        return userstr[:i] if i > 0 else userstr
    
    def refers_to_self(self, name: str):
        """Check if name refers to this bot's irc name/nickname (this calls get_user(name), it can handle 'user!cho@ppy.sh')"""
        ircname = irc_lower(self.connection.ircname)
        nickname = irc_lower(self.connection.get_nickname())
        return irc_lower(self.get_user(name)) in (ircname, nickname)
    
    def refers_to_bot_target(self, name: str):
        """Check if name refers to bot_target (BanchoBot) irc name/nickname (this calls get_user(name), it can handle 'user!cho@ppy.sh')"""
        ircname = irc_lower(self.bot_target)
        return irc_lower(self.get_user(name)) == ircname

    def on_join(self, conn: IRCServerConnection, event: IRCEvent):
        user = self.get_user(event.source)
        log.info(f"'{user}' joined '{event.target}'")
        if self.refers_to_self(user):
            self.channel = str(event.target)
        if self.__creating_room:
            self.room_id = str(event.target)
            self.__init_room()
            # self.room_name = ?
            self.__creating_room = False
        elif self.raw_commands:
            for cmd in self.raw_commands:
                self.send_message(event.target, cmd)
            self.raw_commands = []

    def __on_part(self, conn: IRCServerConnection, event: IRCEvent, reason: str):
        user = self.get_user(event.source)
        log.info(f"'{user}' left channel '{event.target}', reason: {reason}, args: '{' '.join(event.arguments)}'")
        if self.refers_to_self(user):
            self.channel = ''
        if self.__closing_room:
            self.room_id = ''
            self.__closing_room = False
    
    def on_part(self, conn: IRCServerConnection, event: IRCEvent):
        self.__on_part(conn, event, 'part')

    def on_kick(self, conn: IRCServerConnection, event: IRCEvent):
        self.__on_part(conn, event, 'kick')
    
    def on_error(self, conn: IRCServerConnection, event: IRCEvent):
        user = self.get_user(event.source)
        if self.refers_to_self(user):
            log.error(f"onerror, user: '{user}' target: '{event.target}', args: '{' '.join(event.arguments)}'")
        
    def on_motd(self, conn: IRCServerConnection, event: IRCEvent):
        if not self.refers_to_self(event.target): return
        Console.writeln(' '.join(event.arguments))

        # set the motd event flag a bit after the motd stops coming in
        if self.__bot_motd_timer is not None:
            self.__bot_motd_timer.cancel()
        self.__bot_motd_timer = threading.Timer(self.bot_event_delay, lambda: self.__motd_complete())
        self.__bot_motd_timer.start()

    def on_motd2(self, conn: IRCServerConnection, event: IRCEvent):
        self.on_motd(conn, event)

    def on_motdstart(self, conn: IRCServerConnection, event: IRCEvent):
        self.on_motd(conn, event)
    
    def on_endofmotd(self, conn: IRCServerConnection, event: IRCEvent):
        if not self.refers_to_self(event.target): return
        Console.writeln(' '.join(event.arguments))
        self.__motd_complete()

    ## ----------------------------------------------------------------------

    def on_nicknameinuse(self, conn: IRCServerConnection, event: IRCEvent):
        conn.nick(conn.get_nickname() + "_")
    def on_nickcollision(self, conn: IRCServerConnection, event: IRCEvent):
        return self.on_nicknameinuse(conn, event)

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
        # when joining:  src:'cho.ppy.sh', target:'mastaa_p', args:'= #mp_107083661 @BanchoBot +mastaa_p '
        self.do_command(event, f"namreply src:'{event.source}', target:'{event.target}', args:'{' '.join(event.arguments)}', tags:'{' '.join(event.tags)}'")
        # self.do_command(event, event.arguments[0])
        # self.do_command(IRCEvent('stats', '', ''), ' '.join(event.arguments))

    ## ----------------------------------------------------------------------
    # raw message handling

    def on_privmsg(self, conn: IRCServerConnection, event: IRCEvent):
        if not event.arguments: return
        msg = str(event.arguments[0]).strip().lower()
        if not msg: return
        if self.refers_to_bot_target(event.source):
            if 'created' in msg:
                i = msg.find('http://')
                if i == -1: i = msg.find('https://')
                if i != -1:
                    msg = msg[i:]
                    i = msg.find(' ')
                    if i != -1:
                        self.room_link = msg[:i]
                        self.room_name = msg[i:].strip()
                    else:
                        self.room_link = msg.strip()
                        self.room_name = ''
        self.do_command(event, ' '.join(event.arguments))

    def on_privnotice(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, ' '.join(event.arguments))

    def on_pubmsg(self, conn: IRCServerConnection, event: IRCEvent):
        Console.writeln(' '.join(event.arguments))
        # leave event:
        #   source: 'BanchoBot!cho@ppy.sh'
        #   target: '#mp_107081811'
        #   arguments: 'mastaa_p left the game.'
        # close room event:
        #   source: 'BanchoBot!cho@ppy.sh'
        #   target: '#mp_107081811'
        #   arguments: 'Closed the match'
        jknkasf = self.channels
        # mastaa_p joined in slot 1
        # mastaa_p left the game
        a = event.arguments[0].split(":", 1)
        if len(a) > 1 and self.refers_to_self(a[0]):
            self.do_command(event, a[1].strip())
        return
    
    def on_pubnotice(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, ' '.join(event.arguments))

    #def on_dccmsg(self, conn: IRCConnection, event: IRCEvent):
    #    # non-chat DCC messages are raw bytes; decode as text
    #    text = event.arguments[0].decode('utf-8')
    #    conn.privmsg("You said: " + text)

    #def on_dccchat(self, conn: irc.client_aio.AioConnection, event: irc.client_aio.Event):
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

    def __format_channel(self, channel: str):
        if not channel: return ''
        return '#' + channel.strip().strip('#').strip().replace(' ', '_')
    
    def __format_user(self, user: str):
        user = self.get_user(user)
        if not user: return ''
        return user.strip().strip('#').strip().replace(' ', '_')
    
    def send_message(self, channel: str, content: str):
        """Send a message to a channel on the server"""
        self._clear_response_event()
        channel = self.__format_channel(channel)
        self.connection.privmsg(channel, content)

    def send_pm(self, user: str, content: str):
        """Send a private message to a user on the server"""
        self._clear_response_event()
        user = self.__format_user(user)
        self.connection.privmsg(user, content)

    def send_raw(self, content: str):
        """Send a raw string to the server (will be padded with CLRF for you)"""
        self._clear_response_event()
        self.connection.send_raw(content)

    def _clear_response_event(self):
        if self.__bot_response_timer is not None:
            self.__bot_response_timer.cancel()
        self.bot_response_event.clear()

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
            conn.notice(nick, "Not understood: " + cmd)

        # set the event flag a bit after the response stops coming in
        if self.__bot_response_timer is not None:
            self.__bot_response_timer.cancel()
        self.__bot_response_timer = threading.Timer(self.bot_event_delay, lambda: self.bot_response_event.set())
        self.__bot_response_timer.start()

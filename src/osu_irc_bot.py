from __future__ import annotations
from threading import Event
from datetime import datetime
from time import sleep
import pprint
from irc.strings import lower as irc_lower
from irc.client import (
    Connection as IRCConnection,
    ServerConnection as IRCServerConnection,
    Event as IRCEvent
)

from helpers import value_or_fallback, try_int
from text import Unicode, OsuFontNames, align_table
from config import Config, MapInfo, MapChoice, try_get_map_info
from console import Console, log
from irc_bot import BaseOsuIRCBot

class OsuIRCBot(BaseOsuIRCBot):
    def __init__(self, cfg: Config,
                 response_event: Event | None = None,
                 motd_event: Event | None = None,
                 map_infos_populated_event: Event | None = None,
                 **connect_params):
        BaseOsuIRCBot.__init__(
            self,
            cfg=cfg,
            response_event=response_event,
            motd_event=motd_event,
            map_infos_populated_event=map_infos_populated_event,
            **connect_params
        )
        self.channel = value_or_fallback(cfg.initial_channel, '')
        self.channel_password = value_or_fallback(cfg.initial_channel_password, '')
        self.raw_commands = value_or_fallback(cfg.raw_commands, [])

        self.room_id = ''
        self.room_map: MapInfo | None = None
        self.__creating_room = False
        self.__closing_room = False

    def stop(self):
        if self._stopped:
            return
        try:
            if self.room_id:
                self.cancel_motd_event()
                self.clear_response_event()
                # TODO: don't close room if people are still playing...
                self.close_room(warn=False)
                self.response_event.wait(self.cfg.response_timeout)
        except Exception:
            pass
        finally:
            super().stop()
    
    ## ----------------------------------------------------------------------
    # room management

    def __init_room(self, room_id=''):
        room_id = room_id or self.room_id
        if not self.room_id:
            log.error(f"room '{self.cfg.room_name}' is not open!")
            return
        room_id = self._format_channel(room_id)
        self.send_message(room_id, f'!mp password {self.cfg.room_password}')
        self.send_message(room_id, f'!mp set {self.cfg.teammode} {self.cfg.scoremode}')
        self.send_message(room_id, f'!mp map 2538074') # demetori - casket of star
        self.send_message(room_id, f'!mp mods freemod')
        self.send_message(room_id, f'!mp settings') # show current room settings
        if self.raw_commands:
            for cmd in self.raw_commands:
                self.send_bot_command(cmd)
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
        self.clear_response_event()
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
        for ref in self.cfg.refs:
            self.send_message(self.room_id, f"!mp invite {ref}")
        self.send_message(self.room_id, f"!mp addref {' '.join(self.cfg.refs)}") # may need them to be in the room first...

    def refers_to_ref(self, userstr: str):
        """ Check if userstr refers to a user who is a ref for the room """
        user = self.get_user(userstr)
        # TODO: track if refs are added
        return self.refers_to_self(user) or user in self.cfg.refs
    
    def join_channel(self, channel: str, password=''):
        # TODO: parse room id from match links?
        # TODO: should probably format number-only channels like '#mp_<room_id>'
        super().join_channel(channel, password)

    ## ----------------------------------------------------------------------

    def lookup_map(self, label: str):
        return next((m for m in self.cfg.maps if m.label == label), None)

    def choose_map(self, label: str):
        map = self.lookup_map(label)
        log.info(f"choose map: {map}")
        if map is None:
            log.error(f"map is empty! could not find map '{label}'")
            return
        self.send_message(self.room_id, f'!mp map {map.mapid}')

    ## ----------------------------------------------------------------------

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
        is_command = command.startswith(('/', '!'))
        command = '' if not is_command else command[1:]

        if self._handle_command(content):
            return
        
        if self.room_id and not command.startswith('mp make'):
            self.send_message(self.room_id, content)
        else:
            self.send_pm(self.cfg.bot_target, content)
        
        # no response expected for plain messages to a room
        if not is_command:
            self.set_response_event(delay=0)

    def _handle_command(self, content: str, source='@@bot'):
        """ Handle if someone sends a custom command (eg: '!mp map hd2'). \n
        If this isn't a custom command, this method does nothing.

        params:
          `content`: the entire message (eg: '!mp map hd2')
          `source`: The source of this command. Either '@@bot' or username of player in room who sent this.
            Uses '@@bot' to refer to the bot because it would be an invalid username.
        returns:
          `True` if it was a custom command and was handled; otherwise `False`
        """
        content = content.strip()
        command = content.lower()
        is_command = command.startswith(('/', '!'))
        if not is_command:
            return False
        
        command = command[1:]

        if command.startswith(('help', 'mp help')):
            help_lines = [
                '--- extra commands, not in bancho ---',
                '!mp map <label>  choose map based on label configured in the .ini file (eg: !mp map hd1)',
                '!mp map_list     list all maps currently configured (alt: !mp maplist)',
                '!mirror          show alternate download links for the current map',
                '!link            show match history link (alt: !room_link, !roomlink, !mp link)'
            ]
            ref_help_lines = [
                '!mp invite_all   (refs only) invite all players and admins configured in the .ini file (alt: !mp inviteall)'
            ]
            private_help_lines = [
                '!stats           see joined channel stats',
                '!debug, !config  see the current config in the .ini file + joined channel stats',
            ]
            if source == '@@bot' or not self.room_id:
                for msg in help_lines:
                    Console.writeln(msg)
                for msg in ref_help_lines:
                    Console.writeln(msg)
                for msg in private_help_lines:
                    Console.writeln(msg)
                Console.writeln('--- bancho help ---')
                self.send_pm(self.cfg.bot_target, content)
            else:
                for msg in help_lines:
                    self.send_message(self.room_id, msg)
                if self.refers_to_ref(source):
                    for msg in ref_help_lines:
                        self.send_message(self.room_id, msg)
            return True
        
        elif source == '@@bot' and command in ('stats', 'debug', 'config'):
            if command in ('debug', 'config'):
                Console.writeln('--- config ---')
                pp = pprint.PrettyPrinter(indent=2)
                Console.writeln(pp.pformat(self.cfg))
                Console.writeln('--- !stats ---')
            self.do_command(IRCEvent('stats', '', ''), '')
            return True
        
        elif source == '@@bot' and any(command.startswith(f'{prefix} ') for prefix in ('j', 'join')):
            rhs = command[command.find(' ')+1:].strip().split(' ', 1) # rhs = right hand side
            channel, password = rhs if len(rhs) == 2 else (rhs[0], '')
            self.join_channel(channel, password)
            return True
        
        elif source == '@@bot' and any(command.startswith(f'{prefix} ') for prefix in ('p', 'part')): # or command == 'part'):
            channel = command[command.find(' ')+1:]
            self.part_channel(channel)
            return True

        elif (source == '@@bot' or self.refers_to_ref(source)) and command in ('mp inviteall', 'mp invite_all'):
            self.invite_participants()
            return True
        
        elif command in ('mirror', 'mirrors'):
            self.say_mirrors()
            return True
        
        elif command in ('link', 'mp link', 'room_link', 'roomlink') and self.room_link:
            if source == '@@bot':
                Console.writeln(self.room_link)
                self.set_response_event(delay=0)
            else:
                self.send_message(self.room_id, f"Match history available [{self.room_link} here].")
            return True
        
        elif command in ('mp maplist', 'mp map_list', 'maplist', 'map_list'):
            if source == '@@bot':
                self.map_infos_populated_event.wait(1)
                if not self.map_infos_populated_event.is_set():
                    Console.writeln("Waiting (max 30s) for map infos to be populated (this is a one-time cost)")
                    self.map_infos_populated_event.wait(30)
                with Console.LockedWriter() as w:
                    font = 'mono'
                    join_text = ' | '
                    directions = ['left'] + 6*['right'] + 2*['left']
                    headers    = ['label', 'cs', 'hp', 'od', 'ar', 'stars', 'length', 'osu! link', 'map description' + ' '*10 ]
                    rows = [[
                        Unicode.underline2(mc.label),
                        f'{mc.map_info.cs:.1f}' if mc.map_info else '?',
                        f'{mc.map_info.hp:.1f}' if mc.map_info else '?',
                        f'{mc.map_info.od:.1f}' if mc.map_info else '?',
                        f'{mc.map_info.ar:.1f}' if mc.map_info else '?',
                        f'{mc.map_info.difficulty_rating:.1f}*' if mc.map_info else '?',
                        f'{mc.map_info.length//60}:{mc.map_info.length%60:02d}' if mc.map_info else '?',
                        mc.get_osu_link(),
                        mc.description,
                    ] for i, mc in enumerate(self.cfg.maps)]
                    for line in align_table(headers, rows, join_text, directions, font):
                        w.writeln(line)
                self.set_response_event(delay=0)
                return True
            
            elif self.room_id:
                self.map_infos_populated_event.wait(1)
                if not self.map_infos_populated_event.is_set():
                    Console.writeln('Waiting (max 30s) for map infos to be populated (this is a one-time cost)')
                    self.map_infos_populated_event.wait(30)
                    
                font = OsuFontNames.STABLE
                font = 'aller'
                join_text = ' | '
                directions = 2*['left'] + 6*['right'] + ['left']
                headers    = ['label', 'mods', 'cs', 'hp', 'od', 'ar', 'stars', 'length', 'map description' + ' '*10]
                rows = [[
                    f'{i+1:<2}. [{mc.get_osu_link()} {mc.label}]',
                    mc.mods.replace('NF', '').replace('freemod', 'free').replace('  ', ' '),
                    f'{mc.map_info.cs:.1f}' if mc.map_info else '?',
                    f'{mc.map_info.hp:.1f}' if mc.map_info else '?',
                    f'{mc.map_info.od:.1f}' if mc.map_info else '?',
                    f'{mc.map_info.ar:.1f}' if mc.map_info else '?',
                    f'{mc.map_info.difficulty_rating:.1f}*' if mc.map_info else '?',
                    f'{mc.map_info.length//60}:{mc.map_info.length%60:02d}' if mc.map_info else '?',
                    mc.description,
                ] for i, mc in enumerate(self.cfg.maps)]
                
                j = 0
                for i, line in enumerate(align_table(headers, rows, join_text, directions, font)):
                    if i == 0: # header
                        self.send_message(self.room_id, line)
                        sleep(1) # try to ensure header is first
                    else:
                        self.send_message(self.room_id, line)
                    j += 1
                    if j == 3 and i < len(self.cfg.maps)-1:
                        # see https://osu.ppy.sh/wiki/en/Bot_account
                        # Personal accounts can send 10 messages every 5 seconds
                        # Bot accounts can send 300 messages every 60 seconds
                        Console.writeln('wait a bit to avoid irc rate limits (10 msg / 5 secs)...', fg='gray')
                        sleep(3)
                        j = 0
                return True
        
        elif command.startswith(('mp map', 'map')):
            # TODO: restrict players from changing the map?
            # (only makes sense once we implement tracking for who is host, whose turn it is, etc.)
            #if source != '@@bot' and not self.refers_to_ref(source):
            #    self.send_message(self.room_id, 'for now, only refs can change the map')
            #    return True

            aftermap = content[content.index('map')+4:]
            # two valid cases here. The wacky logic is to handle if map label has spaces and ends with a number
            # 1. !mp map <mapid> [<playmode>]
            # 2. !mp map <map label> [<playmode>]
            mapid = aftermap.strip().upper()
            rhs = ''
            map_choice = self.lookup_map(mapid)
            if map_choice is None:
                i = aftermap.rfind(' ')
                if i != -1:
                    mapid = aftermap[:i].upper().strip()
                    rhs = aftermap[i:]
                    map_choice = self.lookup_map(mapid)
            if map_choice is not None:
                mapid = str(map_choice.mapid)
            if self.room_id and map_choice:
                self.send_message(self.room_id, f'!mp map {mapid}{rhs}')
                self.send_message(self.room_id, f'!mp set {map_choice.teammode} {map_choice.scoremode}')
                self.send_message(self.room_id, f'!mp mods {map_choice.mods}')
                return True
            elif source == '@@bot':
                self.send_message(self.room_id, f'!mp map {mapid}{rhs}')
                return True
        
        return False

    ## ----------------------------------------------------------------------
    #  join / welcome / message of the day handling

    def _on_motd_complete(self):
        super()._on_motd_complete()
        if self.cfg.room_name:
            self.create_room()
        elif self.channel:
            self.join_channel(self.channel, self.channel_password)
        elif self.raw_commands:
            for cmd in self.raw_commands:
                self.send_bot_command(cmd)
            self.raw_commands = []

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
                self.send_bot_command(cmd)
            self.raw_commands = []

    def _on_part_with_reason(self, conn: IRCServerConnection, event: IRCEvent, reason: str):
        super()._on_part_with_reason(conn, event, reason)
        user = self.get_user(event.source)
        if self.refers_to_self(user):
            self.channel = ''
        if self.__closing_room:
            self.room_id = ''
            self.room_name = ''
            self.room_link = ''
            self.__closing_room = False
    
    ## ----------------------------------------------------------------------

    #def on_currenttopic(self, conn: IRCServerConnection, event: IRCEvent):
    #    self.do_command(event, f"currenttopic src:'{event.source}', target:'{event.target}', args:'{' '.join(event.arguments)}', tags:'{' '.join(event.tags)}'")

    #def on_topicinfo(self, conn: IRCServerConnection, event: IRCEvent):
    #    self.do_command(event, f"topicinfo src:'{event.source}', target:'{event.target}', args:'{' '.join(event.arguments)}', tags:'{' '.join(event.tags)}'")
        
    #def on_endofnames(self, conn: IRCServerConnection, event: IRCEvent):
    #    self.do_command(IRCEvent('stats', '', ''), ' '.join(event.arguments))
    
    #def on_namreply(self, conn: IRCServerConnection, event: IRCEvent):
        # when joining:  src:'cho.ppy.sh', target:'mastaa_p', args:'= #mp_107083661 @BanchoBot +mastaa_p '
        # self.do_command(event, f"namreply src:'{event.source}', target:'{event.target}', args:'{' '.join(event.arguments)}', tags:'{' '.join(event.tags)}'")
        # self.do_command(event, event.arguments[0])
        # namreply src:'cho.ppy.sh', target:'mastaa_p', args:'= #mp_107344081 @BanchoBot +mastaa_p ', tags:'', msg:'= #mp_107344081 @BanchoBot +mastaa_p '
        #self.do_command(event, ' '.join(event.arguments))
        # self.do_command(IRCEvent('stats', '', ''), ' '.join(event.arguments))

    ## ----------------------------------------------------------------------
    # raw message handling

    def _message_prelude(self, event: IRCEvent) -> str:
        """Generate the appropriate `<time> <user>: ` prelude for the `event`"""
        if not event:
            return ''
        if self.refers_to_server(event.source):
            username_color = '\033[38;5;201m' # intense pink foreground
        else:
            username_color = '\033[38;5;229m' # light yellow foreground

        if event.type in ('privmsg', 'privnotice'):
            user_suffix = ' [priv]: '
        else:
            user_suffix = ': '
        
        return ''.join((
            datetime.now().strftime('%H:%M'), ' ', # time
            username_color,
            self.get_user(event.source), # username who sent the message
            '\033[0m', # reset color
            user_suffix
        ))
    
    def _color_message(self, msg: str, event: IRCEvent) -> str:
        """Add ANSI colors to the `msg` for username highlights, private/puoblic, etc."""
        if not event:
            return msg
        if event.type in ('privmsg', 'privnotice'):
            return ''.join(('\033[38;5;219m',  msg, '\033[0m')) # light pink foreground
        elif not self.refers_to_server(event.source):
            # highlighting for mention of our username
            ircname = irc_lower(self.connection.ircname)
            nickname = irc_lower(self.connection.get_nickname())
            msgl = irc_lower(msg)
            if ircname in msgl or nickname in msgl:
                return ''.join(('\033[38;5;120m', msg, '\033[0m')) # light green foreground
        return msg
    
    def say_mirrors(self):
        """ If `room_map` is known, sends a message to the room with mirror links to download the map """
        if not self.room_id or not self.room_map: return
        mirrors = [self.room_map.get_osu_link(format=True)] + self.room_map.get_mirror_links(format=True)
        msg = 'beatmap mirrors: ' + ' | '.join(mirrors)
        self.send_message(self.room_id, msg)

    def on_privmsg(self, conn: IRCServerConnection, event: IRCEvent):
        if not event.arguments: return
        msg = str(event.arguments[0]).rstrip()
        if not msg: return
        if self.refers_to_ref(event.source):
            # self -> self? this only happens when sending a pm to yourself from irc
            return
        if self.refers_to_server(event.source) and self.refers_to_self(event.target):
            msgl = msg.lower()
            if msgl.startswith('created') and ('match' in msgl or 'room' in msgl):
                i = msgl.find('http://')
                if i == -1: i = msgl.find('https://')
                if i != -1:
                    msgl = msgl[i:]
                    i = msgl.find(' ')
                    if i != -1:
                        self.room_link = msgl[:i]
                        self.room_name = msgl[i:].strip()
                    else:
                        self.room_link = msgl.strip()
                        self.room_name = ''
            elif msgl.startswith('closed') and ('match' in msgl or 'room' in msgl):
                self.room_link = ''
                self.room_id = ''
                self.room_map = None
                self.room_name = ''
        msg2 = self._message_prelude(event) + self._color_message(msg, event)
        self.do_command(event, msg2)

    def on_pubmsg(self, conn: IRCServerConnection, event: IRCEvent):
        if not event.arguments: return
        msg = str(event.arguments[0]).rstrip()
        if not msg: return

        if self.room_id and self.refers_to_server(event.source) and str(event.target).lower() == self.room_id.lower():
            # when the beatmap changes in the room
            # 21:29 BanchoBot: Beatmap changed to: Cartoon - Why We Lose (ft. Coleman Trapp) [Hobbes2's Light Insane] (https://osu.ppy.sh/b/1291655)
            # 22:23 BanchoBot: Changed beatmap to https://osu.ppy.sh/b/2538074 Demetori - Hoshi no Utsuwa ~ Casket of Star
            msgl = msg.lower()
            if msgl.startswith('closed') and ('match' in msgl or 'room' in msgl):
                self.room_link = ''
                self.room_id = ''
                self.room_map = None
                self.room_name = ''
            elif msgl.startswith(('beatmap changed', 'changed beatmap')):
                self.room_map = None
                i = msgl.rfind('http')
                if i >= 0:
                    j = msgl.find(')', i)
                    if j < 0: j = msgl.find(' ', i)
                    if j < 0: j = len(msgl)
                    link = msgl[i:j]
                    i = link.rfind('/')
                    if i >= 0:
                        mapid_str = link[i+1:]
                        mapid = try_int(mapid_str, None)
                        if mapid is not None:
                            self.room_map = try_get_map_info(self.cfg, mapid)
                self.say_mirrors()
        else:
            self._handle_command(msg, event.source)

        msg2 = self._message_prelude(event) + self._color_message(msg, event)
        self.do_command(event, msg2)
        return
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
    
    def on_privnotice(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, ' '.join(event.arguments))

    def on_pubnotice(self, conn: IRCServerConnection, event: IRCEvent):
        self.do_command(event, ' '.join(event.arguments))

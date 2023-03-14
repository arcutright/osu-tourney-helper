import multiprocessing
from multiprocessing.synchronize import Event as MpEvent
from typing import Union
import pprint
from irc.strings import lower as irc_lower
from irc.client import (
    Connection as IRCConnection,
    ServerConnection as IRCServerConnection,
    Event as IRCEvent
)

from helpers import value_or_fallback
from config import Config
from console import Console, log
from irc_bot import BaseOsuIRCBot

class OsuIRCBot(BaseOsuIRCBot):
    def __init__(self, cfg: Config,
                 response_event: "Union[MpEvent, None]" = None,
                 motd_event: "Union[MpEvent, None]" = None,
                 map_infos_populated_event: "Union[MpEvent, None]" = None,
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
        self.__creating_room = False
        self.__closing_room = False

    def stop(self):
        if self._stopped:
            return
        try:
            if self.room_id:
                self.cancel_motd_event()
                self.clear_response_event()
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
        map = None
        is_map_request = False
        is_command = command.startswith(('/', '!'))
        command = '' if not is_command else command[1:]

        if command.startswith(('help', 'mp help')):
            Console.writeln('--- extra commands, not in bancho ---')
            Console.writeln('!stats             see joined channel stats')
            Console.writeln('!debug, !config    see the current config in the .ini file + joined channel stats')
            Console.writeln('!mp invite_all     invite all players and admins configured in the .ini file')
            Console.writeln('!mp map <label>    choose map based on label configured in the .ini file (eg: !mp map hd1)')
            Console.writeln('!mp map_list       list all maps currently configured')
            Console.writeln('--- bancho help ---')
            self.send_pm(self.cfg.bot_target, content)
            return
        
        elif command.startswith(('stats', 'debug', 'config')):
            if command.startswith(('debug', 'config')):
                Console.writeln('--- config ---')
                pp = pprint.PrettyPrinter(indent=2)
                Console.writeln(pp.pformat(self.cfg))
                Console.writeln('--- !stats ---')
            self.do_command(IRCEvent('stats', '', ''), '')
            return
        
        elif command.startswith(('mp maplist', 'mp map_list')):
            self.map_infos_populated_event.wait(1)
            if not self.map_infos_populated_event.is_set():
                Console.writeln("Waiting (max 30s) for map infos to be populated (this is a one-time cost)")
                self.map_infos_populated_event.wait(30)
            Console.writeln(f"{'label':<8}  {'mapid':<9}  {'mods':<12}  {'map link':<38}  {'map description'}")
            Console.writeln(f"{'-'*8}  {'-'*9}  {'-'*12}  {'-'*38} {'-'*20}")
            for map in self.cfg.maps:
                Console.writeln(f"{map.label:<8}  {map.mapid:>9}  {map.mods:<12}  {map.get_osu_link():<38}  {map.description}")
            self.set_response_event(delay=0)
            return
        
        elif command.startswith(('mp inviteall', 'mp invite_all')):
            self.invite_participants()
            return
        
        elif command.startswith('mp map'):
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

        if self.room_id and not command.startswith('mp make'):
            self.send_message(self.room_id, content)
        else:
            self.send_pm(self.cfg.bot_target, content)

        if is_map_request and self.room_id and map:
            self.send_message(self.room_id, f'!mp set {self.cfg.teammode} {self.cfg.scoremode}')
            self.send_message(self.room_id, f'!mp mods {map.mods}')
            mirrors = map.get_mirror_links()
            if mirrors:
                self.send_message(self.room_id, "------ mirror links -------")
                self.send_message(self.room_id, ' | '.join(mirrors))

        # no response expected for plain messages to a room
        if not is_command:
            self.set_response_event(delay=0)

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

    def on_privmsg(self, conn: IRCServerConnection, event: IRCEvent):
        if not event.arguments: return
        msg = str(event.arguments[0]).rstrip()
        if not msg: return
        if self.refers_to_server(event.source) and self.refers_to_self(event.target):
            msgl = msg.lower()
            if 'created' in msgl:
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
        self.do_command(event, f"{self.get_user(event.source)}: {msg}")

    def on_pubmsg(self, conn: IRCServerConnection, event: IRCEvent):
        if not event.arguments: return
        msg = str(event.arguments[0]).rstrip()
        if not msg: return
        if not self.refers_to_server(event.source):
            # TODO: name highlighting
            pass
        self.do_command(event, f"{self.get_user(event.source)}: {msg}")
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

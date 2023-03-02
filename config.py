import os
import sys
import traceback
import argparse
import configparser
import logging
from dataclasses import dataclass, field
import jaraco.logging
import urllib.error, urllib.request, urllib.response, urllib.parse
import json
import ssl
from http.client import HTTPResponse
from datetime import datetime, timedelta
import dateutil.parser
from console import Console, log

@dataclass
class MapInfo:
    mapid: int
    setid: int
    diff_name: str
    song_title: str
    song_artist: str
    set_creator: str
    mode: int
    bpm: float
    ar: float
    od: float
    cs: float
    hp: float
    length: int
    difficulty_rating: float
    is_ranked: bool
    last_updated: datetime | None = None

@dataclass
class MapChoice:
    label: str
    mapid: int
    mods: str
    description: str
    """ Friendly map string, like 'artist - title [diff] (creator)' """
    map_info: MapInfo | None = None
    """ All map info from public apis, if available """

    def get_osu_link(self):
        """ Get a link to download beatmap from osu! """
        if not self.mapid: return ""
        return f"https://osu.ppy.sh/beatmaps/{self.map_info.mapid}"
    
    def get_mirror_links(self):
        """ Get mirror links to download beatmap """
        if not self.map_info: return []
        return [f"https://kitsu.moe/api/d/{self.map_info.setid}",
                f"https://api.chimu.moe/v1/download/{self.map_info.setid}",
                f"https://beatconnect.io/b/{self.map_info.setid}",
                f"https://api.nerinyan.moe/d/{self.map_info.setid}"]

@dataclass
class Config:
    # irc credentials
    username: str
    password: str
    nickname: str
    # room settings
    room_name: str = 'my tournament room'
    room_password: str = 'placeholder'
    teammode: int = 0  # 0: head to head, 1: tag coop, 2: team vs, 3: tag team vs
    scoremode: int = 3   # 0: score, 1: accuracy, 2: combo, 3: score v2
    always_use_nf: bool = True
    raw_commands: list[str] = field(default_factory=list)
    # referees, players, maps
    refs: set[str] = field(default_factory=set)
    players: set[str] = field(default_factory=set)
    maps: list[MapChoice] = field(default_factory=list)
    # irc settings
    bot_target: str = 'BanchoBot'
    server: str = 'irc.ppy.sh'
    port: int = 6667
    initial_channel: str = ''
    initial_channel_password: str = ''
    tls: bool = False
    response_timeout: float = 5.0
    motd_timeout: float = 3.0
    event_delay_timeout: float = 0.5
    # debug settings
    log_level: str = 'INFO'
    enable_console_colors: bool = True

class QuoteStrippingConfigParser(configparser.ConfigParser):
    def get(self, section, option, *, raw=False, vars=None, fallback=configparser._UNSET):
        val = configparser.ConfigParser.get(self, section, option, raw=raw, vars=vars, fallback=fallback)
        return val.strip().strip('"').strip('\'')

# context to ignore ssl cert issues
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

def parse_datetime(datestr: str) -> datetime | None:
    try:
        return dateutil.parser.parse(datestr)
    except Exception:
        try:
            return datetime.strptime(datestr, '%Y-%m-%dT%H:%M:%S.%fZ')
        except Exception:
            return None

def try_json_get(url: str, lower_keys=True):
    try:
        log.debug(f"try to hit {url}")
        headers = {
            'User-Agent' : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/110.0',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        req = urllib.request.Request(url, headers=headers, method='GET')
        resp: HTTPResponse = urllib.request.urlopen(req, context=ssl_ctx)
        if resp.status != 200:
            return {}
        data: bytes = resp.read()
        raw_json = json.loads(data.decode())
        if not lower_keys:
            return raw_json
        else:
            return {key.lower(): raw_json[key] for key in raw_json}
    except urllib.error.HTTPError as err:
        if err.code == 404:
            log.debug(f"Not found: {url}")
            return {}
        elif err.code >= 400 and err.code < 500:
            return {}
        log.warn(f"{url} : {err.code} {err.reason}")
        return {}
    except Exception as ex:
        log.warn(f"Unexpected error from {url}: {ex}")
        return {} 

def get_many(d: dict, *keys, default=None):
    return next((d[key] for key in keys if key in d), default)
 
def try_get_map_info(mapid: int, label: str = '') -> MapInfo | None:
    """Try to get a map's info (set name, diff name, etc.) from public apis"""
    try:
        ar = -1; hp = -1; od = -1; cs = -1; bpm = -1; length = -1; mode = 0
        _mapid = 0; setid = 0
        sr = 0.0
        diff_name = ''; song_artist = ''; song_title = ''; set_creator = ''
        is_ranked = None; last_updated = None

        map_data = (try_json_get(f"https://api.chimu.moe/v1/map/{mapid}")
                 or try_json_get(f"https://kitsu.moe/api/b/{mapid}"))
        if not map_data:
            log.warn(f"Unable to find map data for '{label}', mapid: {mapid}. Does this exist? https://osu.ppy.sh/beatmaps/{mapid}")
        set_data = {}
        if map_data:
            ar = float(map_data.get('ar', -1))
            hp = float(map_data.get('hp', -1))
            od = float(map_data.get('od', -1))
            cs = float(map_data.get('cs', -1))
            bpm = float(map_data.get('bpm', -1))

            length = get_many(map_data, 'hitlength', 'hit_length', 'totallength', 'total_length', 'length', default=-1)
            mode = map_data.get('mode', -1)
            _mapid = get_many(map_data, 'beatmapid', 'id', default=-1)
            setid = get_many(map_data, 'parentsetid', 'beatmapsetid', 'setid', default=-1)
            diff_name = get_many(map_data, 'diffname', 'diff_name', 'version', default='')
            sr = float(get_many(map_data, 'difficultyrating', 'difficulty_rating', 'difficulty', 'starrating', 'star_rating', default=-1))
            if setid != -1:
                set_data = (try_json_get(f"https://api.chimu.moe/v1/set/{setid}")
                         or try_json_get(f"https://kitsu.moe/api/s/{setid}")
                         or try_json_get(f"https://api.nerinyan.moe/search?q={setid}"))
                if set_data:
                    song_artist = set_data.get('artist', '')
                    song_title = set_data.get('title', '')
                    set_creator = set_data.get('creator', '')
                    is_ranked = get_many(set_data, 'rankedstatus', 'ranked', default=0) == 1
                    last_updated = parse_datetime(set_data.get('lastupdate', ''))
        if map_data and set_data and _mapid == mapid and setid != -1:
            return MapInfo(
                mapid=mapid, setid=setid, diff_name=diff_name, song_title=song_title, song_artist=song_artist,
                set_creator=set_creator, mode=mode, bpm=bpm, ar=ar, od=od, cs=cs, hp=hp, length=length,
                difficulty_rating=sr,
                is_ranked=is_ranked, last_updated=last_updated
            )
    except Exception as ex:
        log.error(traceback.format_exc())
    return None

def try_populate_map_info(map: MapChoice):
    if not map or map.map_info: return
    desc = ''
    mi = try_get_map_info(map.mapid, map.label)
    if mi:
        desc = f"{mi.song_artist} - {mi.song_title} [{mi.diff_name}] ({mi.set_creator})"
    map.description = desc
    map.map_info = mi
    return mi

def parse_config() -> Config:
    # read command line args
    argparser = argparse.ArgumentParser()
    argparser.add_argument('username', default='', nargs='?')
    argparser.add_argument('password', default='', help="osu! irc password (not your osu! password, go to https://osu.ppy.sh/p/irc)", nargs='?')
    argparser.add_argument('--nickname', default='', required=False)
    argparser.add_argument('-i', '--ini', default='tourney.ini', help="path to ini file", required=False)
    jaraco.logging.add_arguments(argparser, default_level=logging.INFO)
    args = argparser.parse_args()

    cfg = Config(username=args.username, password = args.password, nickname = args.nickname)

    # read ini file for defaults
    if not os.path.exists(args.ini):
        sys.stderr.write("Error: You must provide your osu! username\n")
        exit(404)

    cfgparser = QuoteStrippingConfigParser(allow_no_value=True)
    cfgparser.read(args.ini)

    # [credentials] section
    cfg.username = cfgparser.get('credentials', 'osu_username', fallback=cfg.username).replace(' ', '_')
    cfg.password = cfgparser.get('credentials', 'irc_password', fallback=cfg.password).replace(' ', '_')
    cfg.nickname = cfgparser.get('credentials', 'irc_nickname', fallback=cfg.nickname).replace(' ', '_')
    if cfg.nickname and cfg.nickname != cfg.username:
        print("osu! irc currently doesn't support nicknames for irc. Using username instead.")
    cfg.nickname = cfg.username

    # [room] section
    cfg.room_name = cfgparser.get('room', 'room_name', fallback=cfg.room_name)
    cfg.room_password = cfgparser.get('room', 'room_password', fallback=cfg.room_password)
    cfg.teammode = cfgparser.getint('room', 'teammode', fallback=cfg.teammode) 
    cfg.scoremode = cfgparser.getint('room', 'scoremode', fallback=cfg.scoremode)
    cfg.always_use_nf = cfgparser.getboolean('room', 'always_use_nf', fallback=cfg.always_use_nf)

    # [irc.timeouts] section
    cfg.response_timeout = cfgparser.getfloat('irc.timeouts', 'response_timeout', fallback=cfg.response_timeout)
    cfg.motd_timeout = cfgparser.getfloat('irc.timeouts', 'motd_timeout', fallback=cfg.motd_timeout)
    cfg.event_delay_timeout = cfgparser.getfloat('irc.timeouts', 'event_delay_timeout', fallback=cfg.event_delay_timeout)

    # [irc.connection] section
    cfg.server = cfgparser.get('irc.connection', 'server', fallback=cfg.server)
    cfg.port = cfgparser.getint('irc.connection', 'port', fallback=cfg.port)
    cfg.tls = cfgparser.getboolean('irc.connection', 'tls', fallback=cfg.tls)
    cfg.bot_target = cfgparser.get('irc.connection', 'bot_target', fallback=cfg.bot_target).replace(' ', '_')
    
    # [startup] section
    cfg.raw_commands = []
    raw_commands = cfgparser.get('startup', 'raw_commands', fallback=cfgparser.get('startup', 'commands', fallback=''))
    if raw_commands:
        cfg.raw_commands = [s.strip() for s in raw_commands.splitlines()]
    cfg.initial_channel = cfgparser.get('startup', 'initial_channel', fallback=cfg.initial_channel).replace(' ', '_')
    if not cfg.initial_channel:
        cfg.initial_channel = cfgparser.get('startup', 'channel', fallback=cfg.initial_channel).replace(' ', '_')
    cfg.initial_channel_password = cfgparser.get('startup', 'initial_channel_password', fallback=cfg.initial_channel_password)
    if not cfg.initial_channel_password:
        cfg.initial_channel_password = cfgparser.get('startup', 'channel_password', fallback=cfg.initial_channel_password)
    
    # [debug] section
    cfg.enable_console_colors = cfgparser.getboolean('debug', 'enable_console_colors', fallback=cfg.enable_console_colors)
    log_level: str = cfgparser.get('debug', 'log_level', fallback='')
    if log_level and log_level.upper() in ['CRITICAL', 'FATAL', 'ERROR', 'WARN', 'WARNING', 'INFO', 'DEBUG']:
        cfg.log_level = log_level
    else:
        log.warn(f"Log level '{log_level}' not recognized, defaulting to INFO")
    
    # [maps] section
    cfg.maps = []
    if 'maps' in cfgparser:
        for rawlabel in cfgparser['maps']:
            mapid = cfgparser.getint("maps", rawlabel)
            if mapid is None: continue
            label = rawlabel.upper().strip()
            if ('FM' in label) or ('FREE' in label) or ('FREEMOD' in label):
                mods = ['freemod']
            elif ('TB' in label) or ('TIEBREAKER' in label):
                mods = ['freemod']
            else:
                mods = []
                if ('NM' not in label) and ('NOMOD' not in label):
                    if 'HD' in label: mods.append('HD')
                    if 'FI' in label: mods.append('FI')
                    if 'HR' in label: mods.append('HR')
                    if 'DT' in label: mods.append('DT')
                    if 'NC' in label: mods.append('NC')
                    if 'HT' in label: mods.append('HT')
                    if 'EZ' in label: mods.append('EZ')
                    if 'FL' in label: mods.append('FL') # flashlight
                    if 'SO' in label: mods.append('SO') # spunout
                    if 'SD' in label: mods.append('SD') # sudden death
                    if 'PF' in label: mods.append('PF') # perfect
                    if 'AP' in label: mods.append('AP') # autopilot
                    if 'RL' in label: mods.append('RL') # relax
                    # if 'AT' in label: mods.append('AT') # auto (watch a playthrough)
                    # if 'CM' in label: mods.append('CM') # cinema
                    # if 'TP' in label: mods.append('TP') # target practice
                    if '3MOD' in label:
                        if 'HD' not in mods: mods.append('HD')
                        if 'HR' not in mods: mods.append('HR')
                        if 'DT' not in mods and 'NC' not in mods: mods.append('DT')
                if cfg.always_use_nf or 'NF' in label: mods.append('NF')
            
            # lookup map description later, on a background thread (see references to try_populate_map_info)
            map = MapChoice(label=label, mapid=mapid, mods=' '.join(mods), description='', map_info=None)
            cfg.maps.append(map)
        pass
    
    # [players] and [refs] sections
    cfg.refs = set()
    cfg.players = set()
    if 'refs' in cfgparser:
        cfg.refs = set([s.strip().replace(' ', '_') for s in cfgparser['refs'] if s])
    if 'players' in cfgparser:
        cfg.players = set([s.strip().replace(' ', '_') for s in cfgparser['players'] if s and s not in cfg.refs])

    # input validation
    if not cfg.username:
        log.fatal("You must provide your osu! username")
        exit(-1)
    if not cfg.password:
        log.fatal("You must provide your osu! irc password (not your osu! password, go to https://osu.ppy.sh/p/irc)")
        exit(-1)

    cfg.nickname = cfg.nickname or cfg.username
    return cfg

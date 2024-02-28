from __future__ import annotations
import os
import sys
import traceback
import argparse
import configparser
import logging
import ast
from dataclasses import dataclass, field
from enum import Enum, Flag, IntFlag
from datetime import datetime, timedelta
import jaraco.logging

from console import Console, log, setup_logging
from helpers import try_int, parse_datetime, get_many, try_json_request, JsonResponse

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
    mods: str = ''
    """If this is non-empty, that means ar/od/cs/hp/sr all include these mods in their calculation"""
    last_updated: datetime | None = None
    
    def get_osu_link(self, format=False) -> str:
        """ Get a link to download beatmap from osu! \n
        `format=True` will make it return '[link alias]' for each link, which shows the alias in the osu! lobby
        """
        return get_osu_link(self.mapid, format)
    
    def get_mirror_links(self, format=False) -> list[str]:
        """ Get mirror links to download beatmap. \n
        `format=True` will make it return '[link alias]' for each link, which shows the alias in the osu! lobby
        """
        if not self.setid: return []
        return get_mirror_links(self.setid, format)

@dataclass
class MapChoice:
    label: str
    """ Uppercase tournament label for map following common conventions. eg: 'HD1', 'DTHR2', etc. """
    alias: str
    """ Optional alias for map label provided by user. Defaults to `label` """
    # labels_lookup: set[str] # TODO: all lowercase + support alt formats, like 'hr 1' -> 'hr1' or 'hrhd1' -> 'hdhr1'
    mapid: int
    mods: str
    teammode: int  # 0: head to head, 1: tag coop, 2: team vs, 3: tag team vs
    scoremode: int  # 0: score, 1: accuracy, 2: combo, 3: score v2
    description: str
    """ Friendly map string, eg 'artist - title [diff] (creator)' """
    map_info: MapInfo | None = None
    """ All map info from public apis, if available """
    
    def get_osu_link(self, format=False) -> str:
        """ Get a link to download beatmap from osu! \n
        `format=True` will make it return '[link alias]' for each link, which shows the alias in the osu! lobby
        """
        return get_osu_link(self.mapid, format)
    
    def get_mirror_links(self, format=False) -> list[str]:
        """ Get mirror links to download beatmap. \n
        `format=True` will make it return '[link alias]' for each link, which shows the alias in the osu! lobby
        """
        if not self.map_info: return []
        return get_mirror_links(self.map_info.setid, format)
    
@dataclass
class BearerToken:
    access_token: str
    expires_utc: datetime

@dataclass
class OsuAPIv2Credentials:
    enabled: bool = True
    client_id: int = 0
    client_secret: str = ''
    token: BearerToken = None
    token_failed = False

@dataclass
class Config:
    # irc credentials
    username: str
    password: str
    nickname: str = ''
    # api credentials
    osu_apiv2_credentials: OsuAPIv2Credentials = field(default_factory=OsuAPIv2Credentials)
    # room settings
    room_name: str = 'my tournament room'
    room_password: str = 'placeholder'
    teammode: int = 0  # 0: head to head, 1: tag coop, 2: team vs, 3: tag team vs
    scoremode: int = 3  # 0: score, 1: accuracy, 2: combo, 3: score v2
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
    event_delay_timeout: float = 0.8
    # misc settings
    log_level: int | str = 'INFO'
    enable_console_colors: bool = True
    max_history_lines: int = 200


def get_osu_link(mapid: int, format=False) -> str:
    """ Get a link to download beatmap from osu! \n
    `format=True` will make it return '[link alias]' for each link, which shows the alias in the osu! lobby
    """
    if not mapid: return ""
    link = (f"https://osu.ppy.sh/b/{mapid}", "osu.ppy.sh")
    return f"[{link[0]} {link[1]}]" if format else link[0]

def get_mirror_links(setid: int, format=False) -> list[str]:
    """ Get mirror links to download mapset. \n
    `format=True` will make it return '[link alias]' for each link, which shows the alias in the osu! lobby
    """
    if not setid: return []
    links = [
        (f"https://api.chimu.moe/v1/download/{setid}", "chimu.moe"),
        (f"https://kitsu.moe/api/d/{setid}", "kitsu.moe", ),
        (f"https://beatconnect.io/b/{setid}", "beatconnect.io"),
        (f"https://api.nerinyan.moe/d/{setid}", "nerinyan.moe")
    ]
    if format:
        return [f"[{link[0]} {link[1]}]" for link in links]
    else:
        return [link[0] for link in links]

def try_get_osuv2_credentials(cfg: Config):
    if not cfg: return False
    # TODO: handle refreshes if needed for creds.token
    creds = cfg.osu_apiv2_credentials
    request_data = {
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'grant_type': 'client_credentials',
        'scope': 'public',
    }
    # see https://osu.ppy.sh/docs/index.html
    data = try_json_request(f"https://osu.ppy.sh/oauth/token", method='POST',
                            headers={'Content-Type': 'application/x-www-form-urlencoded'},
                            body=request_data)
    if not data:
        return False
    elif any(k not in data for k in ('access_token', 'expires_in')):
        log.warn(f"Unexpected token format from osu? Received '{data}'")
        return False
    expires = datetime.utcnow() + timedelta(seconds=int(data['expires_in']))
    creds.token = BearerToken(str(data['access_token']), expires)
    return True

def try_get_map_info(cfg: Config, mapid: int, label: str = '', mods: str = '') -> MapInfo | None:
    """Try to get a map's info (set name, diff name, etc.) from public apis"""
    if mapid is None: return None
    try:
        ar = -1; hp = -1; od = -1; cs = -1; bpm = -1; length = -1; mode = 0
        _mapid = 0; setid = 0
        sr = 0.0
        ranked = -1
        diff_name = ''; song_artist = ''; song_title = ''; set_creator = ''
        last_updated = None

        can_use_osu_apiv2 = False
        map_data = {}
        if cfg is not None:
            creds = cfg.osu_apiv2_credentials
            if creds.enabled and not creds.token_failed:
                if not creds.token or creds.token.expires_utc < datetime.utcnow():
                    got_token = try_get_osuv2_credentials(cfg)
                    if not got_token:
                        log.error(
                            "Could not use the osu v2 api. Did you set up the client id/client secret in the config file?\n"
                            "If you don't have one, you have to set it up at https://osu.ppy.sh/home/account/edit#new-oauth-application\n"
                            "Name: 'osu-tourney-helper'\n"
                            "Callback url: <blank>\n"
                            "Then copy the client id/client secret into the config file"
                        )
                        creds.token_failed = True
                if creds.token and not creds.token_failed:
                    # see https://osu.ppy.sh/docs/index.html
                    map_data = try_json_request(
                        f"https://osu.ppy.sh/api/v2/beatmaps/{mapid}",
                        headers={'Authorization': f"Bearer {creds.token.access_token}"}
                    )
                    can_use_osu_apiv2 = map_data is not None
        if not map_data:
            map_data = (try_json_request(f"https://api.chimu.moe/v1/map/{mapid}")
                     or try_json_request(f"https://kitsu.moe/api/b/{mapid}"))
        if not map_data:
            log.warn(f"Unable to find map data for '{label}', mapid: {mapid}. Does this exist? https://osu.ppy.sh/beatmaps/{mapid}")
        set_data = {}
        if map_data:
            # the fallbacks are a way of supporting every api response in one block
            # for instance, most apis use 'od' and 'hp', but osu's apiv2 uses 'accuracy' and 'drain'
            ar = float(get_many(map_data, 'ar', 'approach_rate', default=-1))
            hp = float(get_many(map_data, 'hp', 'drain', default=-1))
            od = float(get_many(map_data, 'od', 'accuracy', 'overall_difficulty', default=-1))
            cs = float(get_many(map_data,'cs', 'circle_size', default=-1))
            bpm = float(get_many(map_data, 'bpm', default=-1))

            length = float(get_many(map_data, 'hitlength', 'hit_length', 'totallength', 'total_length', 'length', default=-1))
            mode = int(get_many(map_data, 'mode_int', 'mode', default=-1))
            _mapid = int(get_many(map_data, 'beatmapid', 'beatmap_id', 'mapid', 'map_id', 'id', default=-1))
            setid = int(get_many(map_data, 'parentsetid', 'parentset_id', 'beatmapsetid', 'beatmapset_id', 'setid', 'set_id', default=-1))
            diff_name = str(get_many(map_data, 'diffname', 'diff_name', 'version', default=''))
            sr = float(get_many(map_data, 'difficultyrating', 'difficulty_rating', 'difficulty', 'starrating', 'star_rating', default=-1))
            ranked = int(get_many(map_data, 'rankedstatus', 'ranked', default=-1))
            last_updated = parse_datetime(str(get_many(map_data, 'last_updated', 'last_update', 'lastupdated', 'lastupdate', default='')))
            passcount = int(get_many(map_data, 'passcount', 'pass_count', default=-1))
            playcount = int(get_many(map_data, 'playcount', 'play_count', default=-1))

            if setid != -1:
                set_data = (map_data.get('beatmapset', None) # osu /beatmaps/mapid comes with the set data
                         or try_json_request(f"https://api.chimu.moe/v1/set/{setid}")
                         or try_json_request(f"https://kitsu.moe/api/s/{setid}")
                         or try_json_request(f"https://api.nerinyan.moe/search?q={setid}"))
                if set_data:
                    song_artist = str(set_data.get('artist', ''))
                    song_title = str(set_data.get('title', ''))
                    set_creator = str(set_data.get('creator', ''))
                    if ranked == -1: ranked = int(get_many(set_data, 'rankedstatus', 'ranked', default=ranked))
                    if passcount == -1: passcount = int(get_many(set_data, 'passcount', 'pass_count', default=passcount))
                    if playcount == -1: playcount = int(get_many(set_data, 'playcount', 'play_count', default=playcount))
                    if not last_updated: last_updated = parse_datetime(str(get_many(set_data, 'last_updated', 'last_update', 'lastupdated', 'lastupdate', default='')))
        
        if map_data and set_data and _mapid == mapid and setid != -1:
            non_difficulty_mods = set(('NF', 'NM', 'FREEMOD', 'SO', 'SD', 'PF', 'AP', 'RL', 'AT', 'CM', 'TP'))
            filtered_mods = [mod for mod in mods.upper().split(' ') if mod and mod not in non_difficulty_mods]

            # estimates / mod effects that aren't captured by the osu scorev2 api
            # https://osu.ppy.sh/wiki/en/Gameplay/Game_modifier
            if 'HR' in filtered_mods:
                if cs != -1: cs = min(10, cs * 1.3)
                if ar != -1: ar = min(10, ar * 1.4)
                if hp != -1: hp = min(10, hp * 1.4)
                if od != -1: od = min(10, od * 1.4)
            elif 'EZ' in filtered_mods:
                if cs != -1: cs /= 2
                if ar != -1: ar /= 2
                if hp != -1: hp /= 2
                if od != -1: od /= 2
            if any(mod in filtered_mods for mod in ('HT', 'DT', 'NC')):
                range300 = 80 - 6 * od  # see https://osu.ppy.sh/wiki/en/Beatmap/Overall_difficulty
                if 'DT' in filtered_mods or 'NC' in filtered_mods:
                    # TODO: DT estimates for hp
                    if od != -1: range300 /= 1.5
                    if length != -1: length /= 1.5
                    if bpm != -1: bpm *= 1.5
                elif 'HT' in filtered_mods:
                    # TODO: HT estimates for hp
                    if od != -1: range300 /= 0.75
                    if length != -1: length /= 0.75
                    if bpm != -1: bpm *= 0.75
                # same formulas work for both HT and DT
                if ar != -1:
                    # see https://github.com/sbrstrkkdwmdr/osumodcalculator/blob/master/index.js
                    if ar > 5:
                        ms = 200 + (11 - ar) * 100
                    else:
                        ms = 800 + (5 - ar) * 80
                    if ms < 300:
                        ar = 11
                    elif ms < 1200:
                        ar = 11 - (ms - 300)/150
                    else:
                        ar = 5 - (ms - 1200)/120
                if od != -1:
                    od = min(11, (80 - range300) / 6)

            if can_use_osu_apiv2 and filtered_mods:
                # grab the difficulty rating in-depth if possible
                # see https://osu.ppy.sh/docs/index.html#beatmapdifficultyattributes
                data = try_json_request(
                    f"https://osu.ppy.sh/api/v2/beatmaps/{mapid}/attributes", method='POST',
                    headers={'Authorization': f"Bearer {creds.token.access_token}"},
                    body={'mods': filtered_mods}
                ) or {}
                attrs = data.get('attributes')
                if attrs:
                    ar = float(get_many(attrs, 'approach_rate', 'ar', default=ar))
                    od = float(get_many(attrs, 'overall_difficulty', 'od', 'accuracy', default=od))
                    sr = float(get_many(attrs, 'star_rating', 'sr', default=sr))

            return MapInfo(
                mapid=mapid, setid=setid, diff_name=diff_name, song_title=song_title, song_artist=song_artist,
                set_creator=set_creator, mode=mode, bpm=bpm, ar=ar, od=od, cs=cs, hp=hp, length=int(round(length)),
                difficulty_rating=sr, mods=mods,
                is_ranked=(ranked==1), last_updated=last_updated
            )
    except Exception as ex:
        log.error(traceback.format_exc())
    return None

def try_populate_map_info(cfg: Config, mc: MapChoice):
    if not mc or mc.map_info: return
    desc = ''
    mi = try_get_map_info(cfg, mc.mapid, mc.label, mc.mods)
    if mi:
        desc = f"{mi.song_artist} - {mi.song_title} [{mi.diff_name}] ({mi.set_creator})"
    mc.description = desc
    mc.map_info = mi
    return mi

class OsuModFlags(Flag):
    # yoinked from https://github.com/ppy/osu-api/wiki
    _None   = 0 # no mods
    NF      = 1
    EZ      = 2
    TD      = 4 # touch device
    HD      = 8
    HR      = 16
    SD      = 32
    DT      = 64
    RL      = 128 # relax
    HT      = 256
    NC      = 512 # Only set along with DoubleTime. i.e: NC only gives 576
    FL      = 1024
    AT      = 2048 # auto (watch a playthrough)
    SO      = 4096
    AP      = 8192  # Autopilot (aka relax2)
    PF      = 16384 # Only set along with SuddenDeath. i.e: PF only gives 16416  
    Key4    = 32768
    Key5    = 65536
    Key6    = 131072
    Key7    = 262144
    Key8    = 524288 
    FI      = 1048576 # fade in (mania)
    Random  = 2097152
    CM      = 4194304 # cinema
    TP      = 8388608 # target practice
    Key9    = 16777216
    KeyCoop = 33554432
    Key1    = 67108864
    Key3    = 134217728
    Key2    = 268435456
    ScoreV2 = 536870912
    Mirror  = 1073741824
    KeyMod  = Key1 | Key2 | Key3 | Key4 | Key5 | Key6 | Key7 | Key8 | Key9 | KeyCoop
    FreeModAllowed = NF | EZ | HD | HR | SD | FL | FI | RL | AP | SO | KeyMod
    ScoreIncreaseMods = HD | HR | DT | FL | FI

def mods_to_flags(mods: str | list[str]):
    if isinstance(mods, str):
        mods = mods.split(' ')
    flags = OsuModFlags._None
    for name in mods:
        if name not in OsuModFlags.__dict__: continue
        flags = flags | OsuModFlags[name]
    return flags

class QuoteStrippingConfigParser(configparser.ConfigParser):
    def get(self, section, option, *, raw=False, vars=None, fallback=configparser._UNSET):
        val = configparser.ConfigParser.get(self, section, option, raw=raw, vars=vars, fallback=fallback)
        return val.strip().strip('"').strip('\'')

def parse_config() -> Config:
    # read command line args
    argparser = argparse.ArgumentParser()
    argparser.add_argument('username', default='', nargs='?')
    argparser.add_argument('password', default='', help="osu! irc password (not your osu! password, go to https://osu.ppy.sh/p/irc)", nargs='?')
    argparser.add_argument('--nickname', default='', required=False)
    argparser.add_argument('-i', '--ini', default='tourney.ini', help="path to ini file", required=False)
    jaraco.logging.add_arguments(argparser, default_level=logging.INFO)
    args = argparser.parse_args()
    setup_logging(args.log_level)

    cfg = Config(username=args.username, password=args.password, nickname=args.nickname, osu_apiv2_credentials=OsuAPIv2Credentials())

    # read ini file for defaults
    if not os.path.exists(args.ini):
        sys.stderr.write("Error: You must provide your osu! username\n")
        exit(404)

    cfgparser = QuoteStrippingConfigParser(allow_no_value=True, interpolation=None)
    cfgparser.read(args.ini)

    # [credentials] section
    cfg.username = cfgparser.get('credentials', 'osu_username', fallback=cfg.username).replace(' ', '_')
    cfg.password = cfgparser.get('credentials', 'irc_password', fallback=cfg.password).replace(' ', '_')
    cfg.nickname = cfgparser.get('credentials', 'irc_nickname', fallback=cfg.nickname).replace(' ', '_')
    if cfg.nickname and cfg.nickname != cfg.username:
        log.warn("osu! irc currently doesn't support nicknames for irc. Using username instead.")
    cfg.nickname = cfg.username

    # [credentials.osu_api_v2] section
    creds = cfg.osu_apiv2_credentials
    creds.enabled = cfgparser.getboolean('credentials.osu_api_v2', 'enabled', fallback=creds.enabled)
    creds.client_id = cfgparser.getint('credentials.osu_api_v2', 'client_id', fallback=creds.client_id)
    creds.client_secret = cfgparser.get('credentials.osu_api_v2', 'client_secret', fallback=creds.client_secret)

    # [room] section
    cfg.room_name = cfgparser.get('room', 'room_name', fallback=cfg.room_name)
    cfg.room_password = cfgparser.get('room', 'room_password', fallback=cfg.room_password)
    # TODO: support friendly names for teammode/scoremode?
    # teammode; 0: head to head, 1: tag coop, 2: team vs, 3: tag team vs
    cfg.teammode = cfgparser.getint('room', 'teammode', fallback=cfg.teammode)
    # scoremode; 0: score, 1: accuracy, 2: combo, 3: score v2
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
    
    # [misc] section
    cfg.enable_console_colors = cfgparser.getboolean('misc', 'enable_console_colors', fallback=cfg.enable_console_colors)
    cfg.max_history_lines = cfgparser.getint('misc', 'max_history_lines', fallback=cfg.max_history_lines)
    log_level: str = cfgparser.get('misc', 'log_level', fallback='')
    if log_level and log_level.upper() in ['CRITICAL', 'FATAL', 'ERROR', 'WARN', 'WARNING', 'INFO', 'DEBUG']:
        cfg.log_level = log_level
    else:
        log.warn(f"Log level '{log_level}' not recognized, defaulting to INFO")
        cfg.log_level = logging.INFO
    setup_logging(cfg.log_level)
    
    # [maps] section
    cfg.maps = []
    if 'maps' in cfgparser:
        for raw_label in cfgparser['maps']:
            label = raw_label.upper().strip()
            alias = label
            try:
                mapid = cfgparser.getint('maps', raw_label, fallback=None)
            except Exception:
                mapid = None
            if mapid is not None:
                teammode = cfg.teammode
                scoremode = cfg.scoremode
            else:
                raw_value = cfgparser.get('maps', raw_label, fallback='')
                try:
                    mapdict: dict = ast.literal_eval(raw_value)
                    alias = mapdict.get('alias', label).strip()
                    mapid = try_int(mapdict.get('mapid', None))
                    teammode = try_int(mapdict.get('teammode', None), cfg.teammode)
                    scoremode = try_int(mapdict.get('scoremode', None), cfg.scoremode)
                except Exception:
                    pass
                if mapid is None:
                    log.warn(f"Unable to parse map '{raw_label}': '{raw_value}'")
                    continue
            
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
            
            map = MapChoice(
                label=label,
                alias=alias,
                mapid=mapid,
                mods=' '.join(mods),
                teammode=teammode,
                scoremode=scoremode,
                # look up map_info and description later, on a background thread
                # (see references to try_populate_map_info)
                description='',
                map_info=None
            )
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
    if not cfg.username or not cfg.password:
        if not cfg.username:
            log.fatal("You must provide your osu! username")
        if not cfg.password:
            log.fatal("You must provide your osu! irc password (not your osu! password, go to https://osu.ppy.sh/p/irc)")
        return None
    
    cfg.nickname = cfg.nickname or cfg.username
    return cfg

# osu! tourney helper

This is an IRC client designed to simplify managing osu! multiplayer tournaments. It tends to follow common osu! tournament conventions to make setup quick for this task. If you are familiar with osu! multiplayer commands, you can easily use this tool.

This tool has its own configuration and defines a handful of custom commands (see `!help`), but you are welcome to use any of the normal osu! irc commands as well (see [osu! tourney management commands](https://osu.ppy.sh/wiki/en/osu%21_tournament_client/osu%21tourney/Tournament_management_commands) and [general osu! irc commands](https://osu.ppy.sh/wiki/en/Community/Internet_Relay_Chat)).


## Configuration

See `tourney.ini` for an example.  The first thing to do is to hit [https://osu.ppy.sh/p/irc](https://osu.ppy.sh/p/irc) to generate your "irc password". Then you can configure everything. Most of `tourney.ini` is heavily commented. Here is an example of the less obvious sections and how they behave.

```ini
# refs are players who are given ref permissions via !addref once they join the room
# note that osu! currently limits you to 8 refs per room
# these should be their osu! usernames, one user per line
[refs]
ref_1
ref_2

# players (not refs)
[players]
player_1
player_2
player_3

# maps: Use unique names for maps. These should follow common osu! tournament naming conventions. Currently this doesn't support truly custom naming conventions.
# 'HDHRDTFL' = valid name
# 'HD4' = valid name
# recognized mods:
#  HD, HR, DT, NC, NF, SO, FL, EZ, NM, FM, TB
#  NM: nomod, FM: freemod, TB: tiebreaker (freemod)
[maps]
NM1 = 3013968
HD1 = 141866
DT2 = 964504
FM1 = 1291655
HDDTHR1 = 2225108
HDDTHR2 = 1786526
TB1 = 3136032
```

Then when you are in the room (*see [Usage](#usage) below for more details*),

`!mp map hd1` would switch to map `141866`, set the `HD` mod, and provide users with a list of mirror links to download the map in case they have trouble downloading it normally.

`!mp inviteall` would invite players `player_1`, `player_2`, ... to the room, along with refs `ref_1`, `ref_2`, ... and automatically give the refs permissions when they join via Bancho's `!mp addref` command.

## Installation

Currently not supported, but I will probably publish pre-compiled releases one day (see the Releases section).

## Usage

Release builds currently don't exist, you can only use this tool if you have python set up and a dev environment prepared (see [requirements](#requirements)).

### Through python

Assuming you meet the [requirements](#requirements)

```sh
# cd to project folder
# activate venv
python osu_tourney_helper.py
```

### Console controls

The "console" acts like a discount shell (bash, cmd, xterm, etc), and has support for some common keyboard controls:

- `<ENTER>` after typing a message/command to send it
- `<UP>` to recall the previous command
- `<DOWN>` to recall the next command
- `<LEFT>`, `<RIGHT>`, `<BACKSPACE>`, `<DEL>`  have their normal functions
- `<CTRL> + <V>` paste text from clipboard
- `<HOME>` and `<END>` to jump to the beginning and end of the command entry. *Note: if the current command entry wraps across multiple lines, eg. if you are typing a long message, this will jump to the beginning and end of the message. Many shells would jump to beginning/end of the current line, but I decided to use the whole command.*
- `<INS>` toggle insert mode (if you are in the middle of a message, this will overwrite chars instead of appending). The "console prompt" will have a yellow asterisk to indicate when you are in insert mode.

### Example: manage a tournament lobby

1. set up the ini file (see [configuration](#configuration))
2. launch program (see [usage](#usage))
3. create and join the room (this happens automatically after it launches if you set up the ini file)
4. `!mp map_list` to view the maplist (or `!mp maplist`)
5. `!mp invite_all` to invite all players and refs to the room (or `!mp inviteall`)
6. `!mp map hdhr1` to change to the map labeled `hdhr1` in the .ini file. This will set up the mods for you and send a list of mirror links to the room in case someone has trouble downloading from osu.ppy.sh.
7. `!mp start [<time>]` to starts the match after a set time (in seconds) or instantaneously if time is not present

For a full list of commands, type `!help` which will show all the custom commands for this program and also send the `!help` command to BanchoBot to grab its help docs. `!mp help` will show the multiplayer-specific bancho help.

Right now, if you close the program via `!q`, `!quit`, or a keyboard interrupt sequence (ctrl+c), it will ensure that any room it creates is automatically closed for you. osu! currently limits you to 4 tournament lobbies at a time, and if you leave you may not be able to re-join to close it... they automatically close after a couple hours of inactivity. This auto-close behavior may or may not be desired.

## Requirements

To run from source, you'll need python 3.10 (developed under 3.10, probably fine to use 3.8+).

```sh
python -m venv .venv

# activate venv
source .venv/bin/activate # linux, mac
.venv\Scripts\activate.bat # windows (cmd)
.venv\Scripts\Activate.ps1 # windows (powershell)

# update pip and install required packages
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Publishing

Publish using nuitka if you want to build a standalone executable.

Requires a c compiler, but for more info see the nuitka site.

```sh
pip install -r requirements-dev.txt 
```
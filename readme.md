## Configuration



## Usage

Assuming you meet the requirements (see below)

```sh
# cd to project folder
# activate venv
python osu_tourney_helper.py
```

### Example: manage a tournament lobby

1. set up the ini file
2. launch program
3. wait for it to create and join the room (this happens automatically if you set up the ini file)
4. `!mp maplist` to verify the maplist
5. `!mp invite_all` to invite all players to the room
6. `!mp map hdhr1` to change to the map labeled `hdhr1` in the .ini file. This will set up the mods for you and send a list of mirror links to the room in case someone has trouble downloading from ppy.sh.
7. `!mp start`

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

pip install nuitka 

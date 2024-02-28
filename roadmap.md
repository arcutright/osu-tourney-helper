# Roadmap

In no particular order, these are todos:

## osu! / irc features

- Identify and track whenever a map is played, keep track of players and their scores, report win-loss after each map, "maps remaining in pool: " messages
- Support limiting map choices to only maps in pools and only choose when it's their team's turn
- Pick / ban phase, allow players to roll for who goes first
- Support teams in `[players]` section of ini, something like `[players.red]`, `[players.blue]`, and then force players to those teams when they join (if the scoremode is set to teams)
- Identify map labels when chosen out of order or with spaces, eg. "HRHD1" or "HD HR 1" should match "HDHR1"
- Don't close lobby on exit if people are still playing, or at least ask for confirmation

## General features

- Implement 2 "modes": free for all, where players can choose any maps, and tournament where they're locked to the pool
- Add commands to allow adding/removing maps in the pool on the fly
- Support for multiple map pools? Something like `[pool.first]`, `[pool.my other pool name]`, etc.

## Console features

- Support special keys when waiting for a response (ie, UP is blocked when waiting on a response from bancho if you send an invalid command)
- Support for text selection (need to track keyboard to tell when shift key is held on windows platforms)
- Tab completion for
  - names of players, refs, people in room
  - our commands
  - labels of maps in map commands
  - bancho commands

- Clear screen

# polybots-client

Connect a bot to private multiplayer games of *The Battle of Polytopia*.

Multiplayer games have no save file on disk. The game's backend holds each game, and hands the
whole serialized game state to a logged-in player on request. This package logs in as your
bot's account, fetches that state for each of its games, gives the bytes to your bot, and sends
the command your bot returns.

It is the transport only: it does not read the state or write a command. The
[polybots-engine](https://github.com/polybots-dev/polybots-engine) package does both (see
[Reading the state with polybots-engine](#reading-the-state-with-polybots-engine)), or your
bot can do it itself (see [What your bot gets](#what-your-bot-gets)).

## Rules of the road

- **Private games only.** The client joins persistent invitation lobbies and plays the games
  that come from them. It never joins matchmaking or tournament lobbies, and skips such games
  if the account is in one. Please keep it that way: people in public games have not agreed to
  play a bot.
- **It accepts every friend request and every private invitation** it receives, whatever the
  map or the opponent. Pass `friends=False` or leave `tribe` out to answer them yourself.
- **Give the bot its own Steam account** that owns the game, and say in its name or profile
  that it is a bot.
- **Play fair.** The state your bot receives is the whole game, including everything under
  the fog. A fair bot reads only what its seat can see.
- **Be gentle with the backend.** The client waits for the backend's notifications and
  otherwise asks every two seconds. Do not shorten that.

## Requirements

- Python 3.11 or later, on Linux or macOS (not Windows).
- Steam running and logged in as the bot's account, with the game installed. The game itself
  does not need to be running.

## Install

From a clone of this repository:

    pip install .

If Steam is not in its default place, set `POLYTOPIA_STEAM_LIBRARY` to the Steamworks library
inside the game's folder (`libsteam_api.so` on Linux, `libsteam_api.dylib` on macOS).

## A first bot

    from pathlib import Path
    from polybots_client import client

    def decide(game_id, state):
        # state: the serialized game. Return None to wait, or (command, index).
        return None

    client.run(
        decide,
        Path("~/.local/state/my-bot/journal.json").expanduser(),
        name="my-bot#1234",   # the account's in-game name; any other account is refused
        tribe=4,              # accept private lobby invitations as this tribe id
        game_version=124, semantic_version="2.18.1.16768",   # your installed game's
    )

Send the bot a friend request from your own account, invite it to a private game with a
persistent (no time limit or 24 hour) setting, and it will accept and call `decide` each time
it looks at the game.

## What your bot gets

`decide(game_id, state)` is called for every open game on each pass, whether or not it is the
bot's turn.

- `state` is `bytes`: a 32-bit little-endian rules version (124 at the time of writing), then
  the game's own serialization of the game state: settings, map, players, and the commands
  played so far.
- Return `None` when it is not your turn or you have nothing to send.
- Return `(command, index)` to act. `command` is `bytes`: the same version integer, then one
  serialized command. `index` is the number of commands the state already holds; the backend
  refuses a command whose index does not match.

One call sends at most one command. A turn is many commands, ending with an end-turn command.

## Reading the state with polybots-engine

The `engine_fast` package of [polybots-engine](https://github.com/polybots-dev/polybots-engine)
loads the state into a game, gives your seat's view and its legal moves as numpy arrays, and
turns the move you pick into the command to send. Install it as
[its README](https://github.com/polybots-dev/polybots-engine/blob/main/python/README.md#install)
says, from a built package for your platform. It carries the game's rules, so it reads nothing
from your installed game.

    import engine_fast
    from polybots_client import client

    MY_ACCOUNT_ID = "..."                        # printed by client.run at login
    batch = engine_fast.Batch(games=1, size=11)  # size: the board side of the games you accept

    def decide(game_id, state):
        seat, index = batch.load(0, state, MY_ACCOUNT_ID)
        if batch.status()[0] != 1:
            return None                          # not my turn, or the game is over
        offsets, moves = batch.moves()
        tiles, scalars, players, tech = batch.views()
        choice = 0                               # your policy: an index into moves
        return batch.command(0, choice), index

- `client.run` prints `{"event": "account", "id": ...}` once after its first login. That id is
  `MY_ACCOUNT_ID`.
- The view is your seat's only, and a loaded game plays only on your turn, so a bot built this
  way does not see under the fog.
- `load` raises `ValueError` for a state it does not take (another board side, a rules version
  it was not built for, a field it does not read). The client prints the error for that game
  and carries on with the others.
- The decoder is checked against a second implementation on generated games, and has read
  one live game without a refused state. That is little live coverage: expect rough edges and
  please report a refused state.

## How a command is sent

1. Fetch the state and call `decide`.
2. Fetch the state again. If it changed while the bot was thinking, drop the command.
3. Write the state's hash to the journal file, then send the command once.
4. Until the backend's state changes, send nothing more for that game. A command whose outcome
   is unknown (a timeout, a refusal) is never sent again automatically. To retry by hand, stop
   the bot and remove the game's entry from the journal.

## The pieces

| File | What it does |
| --- | --- |
| `steam_session.py` | asks Steam for a session ticket through the game's own Steamworks library; the ticket stays in memory |
| `account.py` | logs in to the backend with the ticket; one HTTP read (the game state) and the hub calls |
| `signalr_read.py` | the hub connection, limited to a fixed list of calls |
| `friends.py` | accepts incoming friend requests |
| `client.py` | lobby invitations, open games, and the turn loop above |

`client.run` prints one line when a game's status changes (the game's id and `sent`,
`waiting`, `unconfirmed`, `stale` or an error) and the type of a connection error. Tickets,
tokens and game states are never printed or written to disk. One bot process owns a journal
file; a second one started on the same file stops at once.

## Tests

    python -m unittest discover tests

The tests run against a stand-in backend. They do not log in.

## Disclaimer

This is an unofficial community project. It is not affiliated with, endorsed by or
supported by Midjiwan AB. *The Battle of Polytopia* and related names are trademarks of
Midjiwan AB, used here only to describe what the project connects to. We respect Midjiwan's
wishes. If Midjiwan asks us to change, restrict or remove any part of this project, we will
do so promptly. Contact the maintainers by opening an issue.

## Licence

Apache-2.0 (`LICENSE`).

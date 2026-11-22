"""A bot's turn loop for private multiplayer games: state bytes in, one command out, sent once.

The bot is a function `decide(game_id, state)`. `state` is the game's serialized state as the
backend returns it (the game's own serialization, led by the rules version as a 32-bit integer).
It returns None when it has nothing to send, or `(command, index)`: the serialized command,
led by the same version integer, and the state's command count, which the backend checks.

The state holds the whole game, fog included. A fair bot reads only what its seat can see.
"""

import base64
import fcntl
import hashlib
import json
import struct
import time
import uuid

from .account import Account, get
from .friends import save, tick as accept_friends

NO_LOBBY = str(uuid.UUID(int=0))  # a started game is read by its own id


def private(lobby):
    """A persistent invitation lobby. Matchmaking and tournament lobbies are never joined."""
    return (
        get(lobby, "IsPersistent") is True
        and get(lobby, "MatchmakingGameId") is None
        and get(lobby, "ChallengermodeGameId") is None
    )


def accept_invitations(a, tribe):
    """Join every private lobby that still waits for this account's answer, as `tribe`."""
    lobbies = get(a.call("GetLobbiesInvitations"), "Lobbies")
    if not isinstance(lobbies, list):
        raise ValueError("unsupported lobby reply")
    for lobby in lobbies:
        seats = get(lobby, "Participators") or []
        mine = [p for p in seats if get(p, "UserId") == a.account_id]
        if (
            private(lobby)
            and len(mine) == 1
            and get(mine[0], "InvitationState") == 1
            and tribe not in (get(lobby, "DisabledTribes") or [])
        ):
            a.call(
                "RespondToLobbyInvitation",
                [
                    {
                        "LobbyId": str(uuid.UUID(get(lobby, "Id"))),
                        "Accepted": True,
                        "TribeId": tribe,
                        "TribeSkinId": 0,
                    }
                ],
            )


def games(a):
    """This account's games as (open, ended) id lists, matchmaking games left out."""
    summaries = a.call("GetGameSummariesByParticipation")
    if not isinstance(summaries, list):
        raise ValueError("unsupported game list")
    found = ([], [])
    for g in summaries:
        if get(g, "MatchmakingGameId") is None:
            found[get(g, "DateEnded") is not None].append(str(uuid.UUID(get(g, "GameId"))))
    return found


def state(a, game_id):
    view = a.post("/api/game/get_game_view_model", {"GameId": game_id, "LobbyId": NO_LOBBY})
    if get(view, "Id") != game_id:
        raise ValueError("game identity mismatch")
    raw = base64.b64decode(get(view, "CurrentGameStateData"), validate=True)
    if len(raw) > 4 * 1024 * 1024 or raw[:4] != struct.pack("<i", a.game_version):
        raise ValueError("state of another rules version, or too large")
    return raw


def send(a, game_id, command, index):
    a.call(
        "SendCommand",
        [
            {
                "GameId": game_id,
                "Command": {
                    "SerializedData": base64.b64encode(command).decode(),
                    "CommandIndex": index,
                },
                "CommandCrossControl": 0,
            }
        ],
    )


def turn(a, game_id, journal, path, decide):
    """At most one command for one game. `journal` maps a game to the state its last command
    answered, and is saved before the send, so a command whose outcome is unknown is never sent
    twice: the game waits until the backend's state changes."""
    raw = state(a, game_id)
    digest = hashlib.sha256(raw).hexdigest()
    if journal.get(game_id) == digest:
        return "unconfirmed"
    move = decide(game_id, raw)
    if move is None:
        return "waiting"
    command, index = move
    # Read again after the bot has thought: a command is never sent for an older state.
    if hashlib.sha256(state(a, game_id)).hexdigest() != digest:
        return "stale"
    journal[game_id] = digest
    save(path, journal)
    send(a, game_id, command, index)
    return "sent"


def run(decide, journal_path, name, tribe=None, friends=True, **version):
    """Play every open private game of the account called `name` until interrupted.

    With `tribe`, private lobby invitations are accepted as that tribe. With `friends`,
    incoming friend requests are accepted. `version` is passed to Account.
    """
    journal_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with journal_path.with_suffix(".lock").open("w") as lock:
        # One process owns the journal: two would each send the same command.
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = json.loads(journal_path.read_text()) if journal_path.exists() else {}
        friend_journal = journal_path.with_suffix(".friends.json")
        shown = {}
        while True:
            try:
                with Account(**version) as a:
                    if a.name != name:
                        raise ValueError("unexpected account")
                    if "account" not in shown:
                        # the id a state decoder needs to find this account's seat
                        shown["account"] = a.account_id
                        print(json.dumps({"event": "account", "id": a.account_id}), flush=True)
                    a.call("SubscribeToParticipatingGameSummaries")
                    while True:
                        if friends:
                            accept_friends(a, friend_journal, name)
                        if tribe is not None:
                            accept_invitations(a, tribe)
                        playing, ended = games(a)
                        for game_id in playing:
                            try:
                                status = turn(a, game_id, journal, journal_path, decide)
                            except ValueError as e:
                                # this game's reply or the bot's answer; the other games go on
                                status = "error: " + str(e)[:80]
                            if shown.get(game_id) != status:
                                shown[game_id] = status
                                print(json.dumps({"event": "game", "game": game_id, "status": status}), flush=True)
                        # Only a game the backend reports as ended leaves the journal: a game
                        # missing from one reply may come back with its command still unconfirmed.
                        if set(journal) & set(ended):
                            for game_id in ended:
                                journal.pop(game_id, None)
                            save(journal_path, journal)
                        a.hub.wait_for_activity(2.0)
            except Exception as e:  # noqa: BLE001 - transport errors may carry credentials
                print(json.dumps({"event": "connection_error", "error_type": type(e).__name__}), flush=True)
                time.sleep(30)

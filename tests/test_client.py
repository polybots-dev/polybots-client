import base64
import struct
import tempfile
import unittest
from pathlib import Path

from polybots_client import client

GAME = "00000000-0000-0000-0000-000000000002"
ME = "00000000-0000-0000-0000-000000000001"
V = struct.pack("<i", 124)


class Fake:
    account_id = ME
    game_version = 124

    def __init__(self):
        self.state = V + b"state 0"
        self.calls = []
        self.fail = False

    def post(self, path, payload):
        assert payload == {"GameId": GAME, "LobbyId": client.NO_LOBBY}
        return {"id": GAME, "currentGameStateData": base64.b64encode(self.state).decode()}

    def call(self, method, arguments=None):
        self.calls.append((method, arguments))
        if self.fail:
            raise TimeoutError()
        if method == "GetLobbiesInvitations":
            return {"lobbies": self.lobbies}
        if method == "GetGameSummariesByParticipation":
            return self.summaries


def lobby(**changes):
    row = {
        "id": GAME,
        "isPersistent": True,
        "matchmakingGameId": None,
        "challengermodeGameId": None,
        "disabledTribes": [],
        "participators": [{"userId": ME, "invitationState": 1}, {"userId": "you", "invitationState": 2}],
    }
    return row | changes


class Tests(unittest.TestCase):
    def play(self, a, decide, journal=None):
        journal = {} if journal is None else journal
        with tempfile.TemporaryDirectory() as d:
            return client.turn(a, GAME, journal, Path(d) / "journal.json", decide), journal

    def sends(self, a):
        return [args for method, args in a.calls if method == "SendCommand"]

    def test_a_command_is_sent_once_with_its_index(self):
        a = Fake()
        status, journal = self.play(a, lambda game, raw: (b"move", 7))
        self.assertEqual(status, "sent")
        command = self.sends(a)[0][0]
        self.assertEqual(command["GameId"], GAME)
        self.assertEqual(command["Command"], {"SerializedData": base64.b64encode(b"move").decode(), "CommandIndex": 7})
        # the same state again: the outcome is unknown, so nothing more is sent
        self.assertEqual(self.play(a, lambda game, raw: (b"move", 7), journal)[0], "unconfirmed")
        self.assertEqual(len(self.sends(a)), 1)
        # the backend's state moved on: the next command may go
        a.state = V + b"state 1"
        self.assertEqual(self.play(a, lambda game, raw: (b"next", 8), journal)[0], "sent")
        self.assertEqual(len(self.sends(a)), 2)

    def test_a_failed_send_is_not_repeated(self):
        a = Fake()
        a.fail = True
        journal = {}
        with self.assertRaises(TimeoutError):
            self.play(a, lambda game, raw: (b"move", 0), journal)
        a.fail = False
        self.assertEqual(self.play(a, lambda game, raw: (b"move", 0), journal)[0], "unconfirmed")
        self.assertEqual(len(self.sends(a)), 1)

    def test_no_command_for_a_state_that_changed_while_deciding(self):
        a = Fake()

        def decide(game, raw):
            a.state = V + b"state 1"
            return b"move", 0

        self.assertEqual(self.play(a, decide)[0], "stale")
        self.assertEqual(self.sends(a), [])

    def test_a_bot_with_nothing_to_send_waits(self):
        a = Fake()
        self.assertEqual(self.play(a, lambda game, raw: None), ("waiting", {}))
        self.assertEqual(a.calls, [])

    def test_only_private_pending_invitations_are_accepted(self):
        refused = [
            lobby(isPersistent=False),
            lobby(matchmakingGameId=GAME),
            lobby(challengermodeGameId="x"),
            lobby(disabledTribes=[4]),
            lobby(participators=[{"userId": ME, "invitationState": 2}]),
            lobby(participators=[{"userId": "you", "invitationState": 1}]),
        ]
        for row in refused:
            a = Fake()
            a.lobbies = [row]
            client.accept_invitations(a, 4)
            self.assertEqual(len(a.calls), 1, row)
        a = Fake()
        a.lobbies = [lobby()]
        client.accept_invitations(a, 4)
        self.assertEqual(
            a.calls[1],
            ("RespondToLobbyInvitation", [{"LobbyId": GAME, "Accepted": True, "TribeId": 4, "TribeSkinId": 0}]),
        )

    def test_games_are_split_into_open_and_ended_without_matchmaking(self):
        a = Fake()
        a.summaries = [
            {"gameId": GAME, "dateEnded": None, "matchmakingGameId": None},
            {"gameId": ME, "dateEnded": "2026", "matchmakingGameId": None},
            {"gameId": ME, "dateEnded": None, "matchmakingGameId": ME},
        ]
        self.assertEqual(client.games(a), ([GAME], [ME]))

    def test_tournament_games_are_left_out(self):
        a = Fake()
        a.summaries = [
            {"gameId": GAME, "dateEnded": None, "gameContext": {"externalTournamentId": None}},
            {"gameId": ME, "dateEnded": None, "gameContext": {"externalTournamentId": ME}},
            {"gameId": ME, "dateEnded": None, "gameContext": {"externalMatchId": ME}},
            {"gameId": ME, "dateEnded": None, "gameContext": "unreadable"},
        ]
        self.assertEqual(client.games(a), ([GAME], []))

    def test_tournament_lobbies_are_not_joined(self):
        self.assertTrue(client.private(lobby(gameContext=None)))
        for context in ({"externalTournamentId": ME}, {"externalMatchId": ME}, ["unreadable"]):
            self.assertFalse(client.private(lobby(gameContext=context)))

    def test_a_state_of_another_version_or_size_is_refused(self):
        for raw in (struct.pack("<i", 123) + b"state", V + bytes(4 * 1024 * 1024)):
            a = Fake()
            a.state = raw
            with self.assertRaises(ValueError):
                self.play(a, lambda game, raw: (b"move", 0))
            self.assertEqual(a.calls, [])


if __name__ == "__main__":
    unittest.main()

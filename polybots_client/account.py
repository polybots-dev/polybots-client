"""Dedicated account connection. No credential or raw response logging."""

import base64
import json
import uuid

import requests

from .signalr_read import ReadHub
from .steam_session import SteamSession

ORIGIN = "https://polytopia-prod.net"


def get(obj, name):
    if not isinstance(obj, dict):
        return None
    return next((v for k, v in obj.items() if k.lower() == name.lower()), None)


def outside_matchmaking_and_tournaments(row):
    """A lobby or game summary that is neither matchmaking nor a tournament; an unreadable context counts as one."""
    context = get(row, "GameContext")
    return (
        get(row, "MatchmakingGameId") is None
        and get(row, "ChallengermodeGameId") is None
        and (context is None or isinstance(context, dict))
        and get(context, "ExternalTournamentId") is None
        and get(context, "ExternalMatchId") is None
    )


class Account:
    def __init__(self, game_version=124, semantic_version="2.18.1.16768"):
        """The rules version and build the backend is told; they must be the installed game's."""
        self.game_version, self.semantic_version = game_version, semantic_version

    def __enter__(self):
        self.steam = self.http = self.hub = None
        try:
            self.steam = SteamSession().__enter__()
            self.http = requests.Session()
            ticket = self.steam.ticket()
            with self.http.post(
                ORIGIN + "/api/auth/login_steam",
                json={
                    "GameVersion": self.game_version,
                    "DeviceId": str(uuid.uuid4()),
                    "BundleId": None,
                    "SemanticVersion": self.semantic_version,
                    "SteamAuthTicket": {
                        "Data": base64.b64encode(ticket.data).decode(),
                        "Handle": ticket.handle,
                    },
                },
                timeout=(10, 20),
                allow_redirects=False,
            ) as response:
                response.raise_for_status()
                reply = response.json()
            if get(reply, "Success") is not True:
                raise RuntimeError("backend authentication rejected")
            data = get(reply, "Data")
            self.token = get(data, "JwtToken")
            user = get(data, "User")
            self.account_id = str(uuid.UUID(get(user, "PolytopiaId")))
            self.name = get(user, "UserName") or get(user, "Name")
            self.friend_code = get(user, "FriendCode")
            if not isinstance(self.token, str) or not self.token:
                raise RuntimeError("missing backend credential")
            self.hub = ReadHub(self.http, ORIGIN, self.token).__enter__()
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *args):
        if self.hub:
            self.hub.__exit__()
        if self.http:
            self.http.close()
        if self.steam:
            self.steam.__exit__()
        self.token = None

    def post(self, path, payload):
        if path != "/api/game/get_game_view_model":
            raise ValueError("unsupported authenticated HTTP operation")
        with self.http.post(
            ORIGIN + path,
            json=payload,
            headers={"Authorization": "Bearer " + self.token},
            stream=True,
            timeout=(10, 20),
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > 16 * 1024 * 1024:
                    raise ValueError("response bound")
            result = json.loads(body)
        if get(result, "Success") is not True:
            raise RuntimeError("backend read rejected")
        return get(result, "Data")

    def call(self, method, arguments=None):
        result = self.hub.invoke(method, arguments)
        if get(result, "Success") is not True:
            raise RuntimeError("backend operation rejected")
        return get(result, "Data")

    def friends(self):
        result = self.hub.invoke("GetFriends")
        if get(result, "Success") is not True or not isinstance(
            get(result, "Data"), list
        ):
            raise RuntimeError("friend-list read rejected")
        return get(result, "Data")


if __name__ == "__main__":
    try:
        with Account() as account:
            rows = account.friends()
            print(
                json.dumps(
                    {
                        "authenticated_rules": 124,
                        "account_name": account.name,
                        "friend_code": account.friend_code,
                        "friend_count": len(rows),
                        "friend_statuses": [get(r, "FriendshipStatus") for r in rows],
                        "friend_row_keys": sorted(rows[0]) if rows else [],
                    }
                )
            )
    except Exception as exc:  # noqa: BLE001 - suppress credential-bearing transport errors
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1)

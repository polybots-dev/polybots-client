"""Bounded SignalR transport with an explicit account/lobby/command allowlist."""

import json
import ssl
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import certifi
import websocket

try:
    from .operation_deadline import timeout as phase_timeout
except ImportError:
    from operation_deadline import timeout as phase_timeout


class ProtocolError(RuntimeError):
    pass


class HubTimeout(ProtocolError):
    pass


class ReadHub:
    def __init__(self, http, origin, token):
        self.http, self.origin, self.token = http, origin, token
        self.socket = None
        self.buffer = ""
        self.sequence = 0
        self.activity_pending = False

    def __enter__(self):
        endpoint = self.origin + "/gamehub"
        token = self.token
        for attempt in range(4):
            parts = urlsplit(endpoint)
            query = dict(parse_qsl(parts.query))
            query["negotiateVersion"] = "1"
            negotiate = urlunsplit(
                (
                    parts.scheme,
                    parts.netloc,
                    parts.path.rstrip("/") + "/negotiate",
                    urlencode(query),
                    "",
                )
            )
            r = self.http.post(
                negotiate,
                headers={"Authorization": "Bearer " + token},
                timeout=(phase_timeout(10), phase_timeout(20)),
                allow_redirects=False,
            )
            if r.status_code != 200:
                raise ProtocolError("Hub negotiation HTTP status " + str(r.status_code))
            data = r.json()
            if "url" not in data:
                break
            destination = urlsplit(data["url"])
            # Authenticated service discovery is limited to Azure SignalR's TLS domain.
            if (
                destination.scheme != "https"
                or not destination.hostname
                or not destination.hostname.endswith(".service.signalr.net")
                or destination.username
                or destination.password
                or destination.port not in (None, 443)
            ):
                raise ProtocolError("Unapproved negotiation destination")
            endpoint = data["url"]
            token = data.get("accessToken")
            if not isinstance(token, str) or not token:
                raise ProtocolError("Missing negotiated service token")
        else:
            raise ProtocolError("Too many negotiation redirects")
        connection = data.get("connectionToken") or data.get("connectionId")
        if not isinstance(connection, str) or not connection:
            raise ProtocolError("Missing connection token")
        parts = urlsplit(endpoint)
        query = dict(parse_qsl(parts.query))
        query["id"] = connection
        url = urlunsplit(("wss", parts.netloc, parts.path, urlencode(query), ""))
        self.socket = websocket.create_connection(
            url,
            timeout=phase_timeout(20),
            redirect_limit=0,
            header={"Authorization": "Bearer " + token},
            sslopt={
                "ca_certs": certifi.where(),
                "cert_reqs": ssl.CERT_REQUIRED,
                "check_hostname": True,
            },
        )
        try:
            self.send({"protocol": "json", "version": 1})
            answer = self.receive(time.monotonic() + 20)
            if answer != {}:
                raise ProtocolError("Hub handshake rejected")
            return self
        except BaseException:
            self.socket.close()
            raise

    def send(self, value):
        current = self.socket.gettimeout()
        self.socket.settimeout(phase_timeout(20 if current is None else current))
        self.socket.send(json.dumps(value, separators=(",", ":")) + "\x1e")

    def receive(self, deadline):
        deadline = min(deadline, time.monotonic() + phase_timeout(20))
        if time.monotonic() >= deadline:
            raise HubTimeout("Hub response deadline exceeded")
        while "\x1e" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HubTimeout("Hub response deadline exceeded")
            self.socket.settimeout(remaining)
            chunk = self.socket.recv()
            if not chunk:
                raise ProtocolError("Hub connection closed")
            if not isinstance(chunk, str):
                raise ProtocolError("Unexpected binary hub frame")
            self.buffer += chunk
            if len(self.buffer) > 16 * 1024 * 1024:
                raise ProtocolError("Hub frame exceeds probe limit")
        record, self.buffer = self.buffer.split("\x1e", 1)
        try:
            result = json.loads(record)
        except ValueError:
            raise ProtocolError("Malformed hub record") from None
        if not isinstance(result, dict):
            raise ProtocolError("Invalid hub record shape")
        return result

    def invoke(self, method, arguments=None):
        if method not in (
            "GetGameSummariesByParticipation",
            "SubscribeToParticipatingGameSummaries",
            "GetFriends",
            "AcceptFriendRequest",
            "GetLobbiesInvitations",
            "GetLobby",
            "RespondToLobbyInvitation",
            "SendCommand",
        ):
            raise ProtocolError("Method outside read-only allowlist")
        self.sequence += 1
        invocation = str(self.sequence)
        self.send(
            {
                "type": 1,
                "invocationId": invocation,
                "target": method,
                "arguments": arguments or [],
            }
        )
        deadline = time.monotonic() + 20
        while True:
            msg = self.receive(deadline)
            if msg.get("type") == 3 and msg.get("invocationId") == invocation:
                if "error" in msg:
                    raise ProtocolError(
                        "Hub invocation failed; server detail suppressed"
                    )
                return msg.get("result")
            if msg.get("type") == 7:
                raise ProtocolError("Server closed hub connection")
            if msg.get("type") == 1:
                self.activity_pending = True
            # Notification payloads remain private and are never logged.

    def wait_for_activity(self, seconds=2.0):
        """Wake on server notifications; polling remains the bounded fallback.

        Called only between invocations, never concurrently with a request.
        Partial frames stay in receive's buffer across an idle timeout.
        """
        if self.activity_pending:
            self.activity_pending = False
            return True
        previous_timeout = self.socket.gettimeout() if self.socket else None
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                try:
                    msg = self.receive(deadline)
                except (HubTimeout, websocket.WebSocketTimeoutException):
                    return False
                if msg.get("type") == 7:
                    raise ProtocolError("Server closed hub connection")
                if msg.get("type") == 1:
                    return True
            return False
        finally:
            # A short idle wait must not shorten the next mutation's send budget.
            if self.socket:
                self.socket.settimeout(previous_timeout)

    def __exit__(self, *args):
        if self.socket:
            self.socket.close()

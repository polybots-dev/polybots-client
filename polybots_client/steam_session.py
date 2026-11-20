"""In-memory Steam session tickets using the game's installed Steamworks library.

Pinned to the installed SteamUser_v021 ABI (four-argument ticket function).
Never persists or prints ticket bytes; cancellation is mandatory on exit.
"""

import contextlib
import ctypes as C
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# The game's own Steamworks library (SteamUser021 interface) in the default Steam install.
# POLYTOPIA_STEAM_LIBRARY names it when Steam is installed elsewhere.
GAME = {
    "linux": "~/.steam/debian-installation/steamapps/common/The Battle of Polytopia/Polytopia_Data/Plugins/AnyCPU/libsteam_api.so",
    "darwin": "~/Library/Application Support/Steam/steamapps/common/The Battle of Polytopia/Polytopia.app/Contents/PlugIns/libsteam_api.dylib",
}
LIBRARY = Path(
    os.environ.get("POLYTOPIA_STEAM_LIBRARY")
    or GAME["darwin" if sys.platform == "darwin" else "linux"]
).expanduser()


@contextlib.contextmanager
def quiet_library_output():
    """Suppress the Steam library's diagnostics, which may contain account identifiers."""
    saved = [os.dup(fd) for fd in (1, 2)]
    try:
        with open(os.devnull, "wb") as sink:
            for fd in (1, 2):
                os.dup2(sink.fileno(), fd)
            yield
    finally:
        for fd, original in zip((1, 2), saved):
            os.dup2(original, fd)
            os.close(original)


class SteamUnavailable(RuntimeError):
    pass


class Callback(C.Structure):
    _fields_ = [
        ("user", C.c_int),
        ("kind", C.c_int),
        ("data", C.c_void_p),
        ("size", C.c_int),
    ]


class TicketResponse(C.Structure):
    _fields_ = [("handle", C.c_uint32), ("result", C.c_int)]


@dataclass(repr=False)
class Ticket:
    data: bytes = field(repr=False)
    handle: int


class SteamSession:
    def __enter__(self):
        self.handle = 0
        self.initialized = False
        os.environ["SteamAppId"] = "874390"
        os.environ["SteamGameId"] = "874390"
        self.lib = C.CDLL(str(LIBRARY))

        def bind(name, result, *args):
            fn = getattr(self.lib, name)
            fn.restype = result
            fn.argtypes = list(args)
            return fn

        self.init = bind("SteamAPI_Init", C.c_bool)
        self.shutdown = bind("SteamAPI_Shutdown", None)
        self.user_fn = bind("SteamAPI_SteamUser_v021", C.c_void_p)
        self.logged_in = bind("SteamAPI_ISteamUser_BLoggedOn", C.c_bool, C.c_void_p)
        self.get_ticket = bind(
            "SteamAPI_ISteamUser_GetAuthSessionTicket",
            C.c_uint32,
            C.c_void_p,
            C.c_void_p,
            C.c_int,
            C.POINTER(C.c_uint32),
        )
        self.cancel = bind(
            "SteamAPI_ISteamUser_CancelAuthTicket", None, C.c_void_p, C.c_uint32
        )
        self.dispatch_init = bind("SteamAPI_ManualDispatch_Init", None)
        self.get_pipe = bind("SteamAPI_GetHSteamPipe", C.c_int)
        self.run_frame = bind("SteamAPI_ManualDispatch_RunFrame", None, C.c_int)
        self.next_callback = bind(
            "SteamAPI_ManualDispatch_GetNextCallback",
            C.c_bool,
            C.c_int,
            C.POINTER(Callback),
        )
        self.free_callback = bind(
            "SteamAPI_ManualDispatch_FreeLastCallback", None, C.c_int
        )
        with quiet_library_output():
            initialized = self.init()
        if not initialized:
            raise SteamUnavailable("Steam API initialization failed")
        self.initialized = True
        self.user = self.user_fn()
        if not self.user or not self.logged_in(self.user):
            self.__exit__(None, None, None)
            raise SteamUnavailable("No logged-in Steam user available")
        self.dispatch_init()
        self.pipe = self.get_pipe()
        return self

    def ticket(self, timeout=10.0):
        try:
            from .operation_deadline import timeout as phase_timeout
        except ImportError:
            from operation_deadline import timeout as phase_timeout
        timeout = phase_timeout(timeout)
        if self.handle:
            raise RuntimeError("Only one outstanding ticket per session")
        buffer = C.create_string_buffer(4096)
        size = C.c_uint32()
        self.handle = self.get_ticket(self.user, buffer, len(buffer), C.byref(size))
        if not self.handle or not 0 < size.value <= len(buffer):
            raise SteamUnavailable("Steam ticket creation failed")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.run_frame(self.pipe)
            cb = Callback()
            while self.next_callback(self.pipe, C.byref(cb)):
                try:
                    if cb.kind == 163 and cb.size == C.sizeof(TicketResponse):
                        response = TicketResponse.from_buffer_copy(
                            C.string_at(cb.data, cb.size)
                        )
                        if response.handle == self.handle:
                            if response.result != 1:
                                raise SteamUnavailable("Steam rejected ticket request")
                            return Ticket(bytes(buffer.raw[: size.value]), self.handle)
                finally:
                    self.free_callback(self.pipe)
            time.sleep(0.01)
        raise SteamUnavailable("Steam ticket callback timed out")

    def __exit__(self, *args):
        if self.initialized:
            self.cancel_ticket()
            self.shutdown()
            self.initialized = False

    def cancel_ticket(self):
        if self.handle:
            self.cancel(self.user, self.handle)
            self.handle = 0


if __name__ == "__main__":
    try:
        with SteamSession() as session:
            ticket = session.ticket()
            print(
                "Steam session authenticated; ticket callback succeeded; ticket retained only in memory."
            )
        print("Ticket cancelled and helper session closed.")
    except SteamUnavailable as exc:
        print(str(exc))
        raise SystemExit(1)

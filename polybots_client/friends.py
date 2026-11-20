"""Accept incoming requests only, reconciling every mutation with a fresh list."""

import argparse
import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

from .account import Account, get


def incoming(rows):
    result = []
    for row in rows:
        status = get(row, "FriendshipStatus")
        if type(status) is not int or status not in range(5):
            raise ValueError("unknown friendship status")
        if status == 2:
            result.append(str(uuid.UUID(get(get(row, "User"), "PolytopiaId"))))
    if len(result) != len(set(result)):
        raise ValueError("duplicate incoming request")
    return result


def save(path, data):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def tick(account, path, name):
    """Accept the incoming requests of the account called `name`; any other account is refused."""
    if account.name != name:
        raise ValueError("unexpected account; no changes allowed")
    requests = incoming(account.friends())
    pending = json.loads(path.read_text()) if path.exists() else []
    if not isinstance(pending, list) or any(not isinstance(p, str) for p in pending):
        raise ValueError("invalid request journal")
    # Retain only unconfirmed attempts whose request is still incoming.
    keys = {hashlib.sha256(r.encode()).hexdigest(): r for r in requests}
    pending = [p for p in pending if p in keys]
    save(path, pending)
    accepted = 0
    for key, friend_id in keys.items():
        if key in pending:
            continue  # An uncertain request is never blindly repeated.
        pending.append(key)
        save(path, pending)
        account.hub.invoke("AcceptFriendRequest", [{"FriendUserId": friend_id}])
        fresh = account.friends()
        confirmed = any(
            get(r, "FriendshipStatus") == 3
            and get(get(r, "User"), "PolytopiaId") == friend_id
            for r in fresh
        )
        if confirmed:
            pending.remove(key)
            save(path, pending)
            accepted += 1
    return {"accepted": accepted, "uncertain": len(pending)}


def main(name):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    args.state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # One controller owns the journal even across service restarts.
    import fcntl

    with args.state.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                with Account() as account:
                    print(
                        json.dumps(
                            {
                                "status": "connected",
                                "rules": 124,
                                "account": account.name,
                            }
                        ),
                        flush=True,
                    )
                    while True:
                        result = tick(account, args.state, name)
                        if args.once or result["accepted"] or result["uncertain"]:
                            print(json.dumps(result), flush=True)
                        if args.once:
                            return
                        account.hub.wait_for_activity(2.0)
            except Exception as exc:  # noqa: BLE001 - suppress credential-bearing transport errors
                print(
                    json.dumps(
                        {"status": "connection_error", "error_type": type(exc).__name__}
                    ),
                    flush=True,
                )
                if args.once:
                    raise SystemExit(1)
                time.sleep(30)

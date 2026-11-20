import tempfile
import unittest
from pathlib import Path

from polybots_client.friends import incoming, tick

ID = "00000000-0000-0000-0000-000000000001"


class Fake:
    name = "bot#0001"
    status = 2
    sends = 0
    fail = False

    def __init__(self):
        self.hub = self

    def friends(self):
        return [{"friendshipStatus": self.status, "user": {"polytopiaId": ID}}]

    def invoke(self, method, args):
        assert method == "AcceptFriendRequest"
        assert args == [{"FriendUserId": ID}]
        self.sends += 1
        if self.fail:
            raise TimeoutError()
        self.status = 3
        return {"success": True}


class Tests(unittest.TestCase):
    def test_accept_then_refriend(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "journal.json"
            a = Fake()
            self.assertEqual(tick(a, p, a.name), {"accepted": 1, "uncertain": 0})
            tick(a, p, a.name)
            self.assertEqual(a.sends, 1)
            a.status = 2
            tick(a, p, a.name)
            self.assertEqual(a.sends, 2)

    def test_unknown_outcome_never_resends(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "journal.json"
            a = Fake()
            a.fail = True
            with self.assertRaises(TimeoutError):
                tick(a, p, a.name)
            self.assertEqual(tick(a, p, a.name), {"accepted": 0, "uncertain": 1})
            self.assertEqual(a.sends, 1)
            a.status = 3
            self.assertEqual(tick(a, p, a.name)["uncertain"], 0)

    def test_other_account_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            a = Fake()
            a.name = "someone else"
            with self.assertRaises(ValueError):
                tick(a, Path(d) / "journal.json", "bot#0001")
            self.assertEqual(a.sends, 0)

    def test_only_received_requests(self):
        for status in (0, 1, 3, 4):
            self.assertEqual(incoming([{"friendshipStatus": status}]), [])
        with self.assertRaises(ValueError):
            incoming([{"friendshipStatus": True}])


if __name__ == "__main__":
    unittest.main()

"""Opening a room must publish its new key to the shared HTTP reader."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kanao.errors import ErrorCode, KanaoError
from kanao.keyring import Keyring
from kanao.serve.backends import SharedReads, make_live_room_opener


def ring(keys):
    return Keyring(user_directory=Path("profile"), pid=42, keys=keys)


class RoomReader:
    def __init__(self, current, readable=False):
        self._keyring = current
        self.readable = readable

    @property
    def keyring(self):
        return self._keyring

    def room(self, room_id):
        return SimpleNamespace(id=room_id, readable=self.readable)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class RoomKeyRefreshTests(unittest.TestCase):
    def test_newly_unlocked_room_updates_shared_http_keyring(self):
        reads = SharedReads()
        old = ring({"chat_data/chatLogs_1.edb": b"a"})
        fresh = ring({"chat_data/chatLogs_1.edb": b"a", "chat_data/chatLogs_2.edb": b"b"})
        reads.update_keyring(old)
        reader = RoomReader(old)

        def unlock(target, room):
            self.assertEqual(room.id, 2)
            target._keyring = fresh

        with patch("kanao.opening.open_for_reading", side_effect=unlock) as open_room:
            make_live_room_opener(lambda: reader, on_keys=reads.update_keyring)(2)
        open_room.assert_called_once()
        self.assertEqual(reads._account["keyring"].chat_ids(), [1, 2])

    def test_failed_unlock_does_not_publish_incomplete_keys(self):
        reads = SharedReads()
        old = ring({"chat_data/chatLogs_1.edb": b"a"})
        reads.update_keyring(old)
        error = KanaoError(ErrorCode.KEY_UNAVAILABLE, "key missing")
        with patch("kanao.opening.open_for_reading", side_effect=error):
            with self.assertRaises(KanaoError):
                make_live_room_opener(lambda: RoomReader(old), reads.update_keyring)(2)
        self.assertIs(reads._account["keyring"], old)

    def test_already_readable_room_needs_no_new_scan(self):
        notify = Mock()
        with patch("kanao.opening.open_for_reading") as open_room:
            make_live_room_opener(
                lambda: RoomReader(ring({}), readable=True), notify,
            )(2)
        open_room.assert_not_called()
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()

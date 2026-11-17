"""Regression tests for adaptive, shared SSE key discovery."""
from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from kanao import paths
from kanao.keyring import Keyring
from kanao.serve import backends


DIRECT = paths.chat_log_name(1)
GROUP = paths.chat_log_name(2)


def ring(keys, pid=42):
    return Keyring(user_directory=Path("fake-profile"), pid=pid, keys=keys)


class Reader:
    user_directory = Path("fake-profile")

    def __init__(self, keyring):
        self._keyring = keyring
        self.closed = False

    @property
    def keyring(self):
        return self._keyring

    def close(self):
        self.closed = True


class FakeWatcher:
    def __init__(self, reader, interval):
        self.reader = reader
        self.poll_count = 0
        self.recovered = []

    def watched_rooms(self):
        return self.reader.keyring.chat_ids()

    def prime(self):
        return None

    def resume(self, cursor):
        return None

    def backfill_new_room(self, room, *, since):
        self.recovered.append((room, since))
        return []

    def poll(self):
        self.poll_count += 1
        return []


class KeyRefreshTests(unittest.TestCase):
    def setUp(self):
        self.clock = mock.patch.object(backends.time, "monotonic", return_value=100)
        self.wall = mock.patch.object(backends.time, "time", return_value=1000)
        self.watcher = mock.patch("kanao.watch.Watcher", FakeWatcher)
        self.now = self.clock.start()
        self.wall_time = self.wall.start()
        self.watcher.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.wall.stop)
        self.addCleanup(self.watcher.stop)
        self.first = ring({DIRECT: b"a"})
        self.reader1 = Reader(self.first)
        self.reader2 = Reader(self.first)
        self.factory = backends.make_live_watcher_factory(
            iter([self.reader1, self.reader2]).__next__, interval=1,
        )

    def test_one_scan_is_shared_across_connections_and_new_room_backfilled(self):
        second = ring({GROUP: b"b"})
        with mock.patch("kanao.keyring.Keyring.discover", return_value=second) as scan:
            a = self.factory()
            b = self.factory()
            a.prime()
            b.prime()
            self.now.return_value = 131
            a.poll()
            b.poll()
        scan.assert_called_once_with(Path("fake-profile"), rescan=True)
        for reader in (self.reader1, self.reader2):
            self.assertEqual(reader.keyring.chat_ids(), [1, 2])
            self.assertEqual(reader.keyring.key_for(DIRECT), b"a")
        for conn in (a, b):
            self.assertEqual(conn._watcher.recovered, [(2, 1000)])
            conn.poll()
            self.assertEqual(len(conn._watcher.recovered), 1)

    def test_implicit_first_poll_does_not_replay_existing_rooms(self):
        a = self.factory()
        a.poll()
        a.poll()
        self.assertEqual(a._watcher.recovered, [])
        self.assertEqual(a._watcher.poll_count, 2)

    def test_failed_initial_baseline_can_recover_after_key_becomes_usable(self):
        class FailedBaselineWatcher(FakeWatcher):
            def __init__(self, reader, interval):
                super().__init__(reader, interval)
                self._seen = {1: 10}

        self.reader1._keyring = ring({DIRECT: b"a", GROUP: b"b"})
        with mock.patch("kanao.watch.Watcher", FailedBaselineWatcher):
            a = self.factory()
            a.prime()
            self.assertEqual(a._known_rooms, {1})
            a.poll()
        self.assertEqual(a._watcher.recovered, [(2, 1000)])

    def test_empty_scan_preserves_old_keys_and_backoff(self):
        with mock.patch("kanao.keyring.Keyring.discover", return_value=ring({})) as scan:
            a = self.factory()
            a.prime()
            self.now.return_value = 131
            a.poll()
            self.assertEqual(self.reader1.keyring.chat_ids(), [1])
            self.now.return_value = 151
            a.poll()
            self.assertEqual(scan.call_count, 1)
            self.now.return_value = 192
            a.poll()
            self.assertEqual(scan.call_count, 2)

    def test_sse_discovery_updates_http_cache_without_losing_existing_keys(self):
        reads = backends.SharedReads()
        reads.update_keyring(self.first)
        factory = backends.make_live_watcher_factory(
            lambda: self.reader1, interval=1, on_keys=reads.update_keyring,
        )
        with mock.patch("kanao.keyring.Keyring.discover", return_value=ring({GROUP: b"b"})):
            watcher = factory()
            watcher.prime()
            self.now.return_value = 131
            watcher.poll()
        with reads.reading() as reader:
            self.assertEqual(reader.keyring.chat_ids(), [1, 2])
        reads.update_keyring(ring({GROUP: b"b"}, pid=43))
        with reads.reading() as reader:
            self.assertEqual(reader.keyring.chat_ids(), [2])

    def test_scan_failure_does_not_stop_messages(self):
        with mock.patch("kanao.keyring.Keyring.discover", side_effect=OSError("locked")):
            a = self.factory()
            a.prime()
            self.now.return_value = 131
            with redirect_stderr(io.StringIO()):
                self.assertEqual(a.poll(), [])
            self.assertEqual(a._watcher.poll_count, 1)
            self.assertIs(self.reader1.keyring, self.first)

    def test_process_restart_does_not_reuse_old_keys(self):
        with mock.patch("kanao.keyring.Keyring.discover", return_value=ring({GROUP: b"b"}, pid=43)):
            a = self.factory()
            a.prime()
            self.now.return_value = 131
            a.poll()
            self.assertEqual(self.reader1.keyring.chat_ids(), [2])

    def test_failed_room_recovery_retries_without_killing_sse(self):
        a = self.factory()
        a.prime()
        self.reader1._keyring = ring({DIRECT: b"a", GROUP: b"b"})
        a._watcher.backfill_new_room = mock.Mock(side_effect=OSError("busy"))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(a.poll(), [])
        self.assertEqual(a._watcher.poll_count, 1)
        a._watcher.backfill_new_room.assert_called_once()
        a.poll()
        a._watcher.backfill_new_room.assert_called_once()


if __name__ == "__main__":
    unittest.main()

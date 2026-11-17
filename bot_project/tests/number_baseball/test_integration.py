import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "number-baseball-bot"))

from number_baseball_bot import BaseballBot
from number_baseball_bot import dispatch, is_target_message, iter_sse


class FakeClient:
    def __init__(self):
        self.calls = []

    def send(self, room_id, text):
        self.calls.append((room_id, text))


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.bot = BaseballBot(secret_factory=lambda: "427")
        self.client = FakeClient()
        self.message = {
            "id": "900",
            "roomId": "100",
            "senderId": "42",
            "sender": "테스터",
            "text": "!숫자야구 시작",
            "kind": "text",
            "fromMe": False,
        }

    def test_responds_in_all_rooms_with_separate_games(self):
        self.assertTrue(dispatch(self.client, self.bot, self.message))
        other = dict(self.message, roomId="200")
        self.assertTrue(dispatch(self.client, self.bot, other))
        self.assertEqual([room for room, _ in self.client.calls], ["100", "200"])
        self.assertEqual(set(self.bot.games), {"100:42", "200:42"})
        give_up = dict(self.message, text="!숫자야구 포기")
        self.assertTrue(dispatch(self.client, self.bot, give_up))
        self.assertEqual(set(self.bot.games), {"200:42"})

    def test_unspaced_help_reaches_chat(self):
        message = dict(self.message, text="!숫자야구도움말")
        self.assertTrue(dispatch(self.client, self.bot, message))
        self.assertIn("!숫자야구 시작", self.client.calls[-1][1])

    def test_help_alias_does_not_reply(self):
        for text in ("!숫자야구", "!숫자야구 도움말"):
            with self.subTest(text=text):
                self.assertFalse(dispatch(self.client, self.bot, dict(self.message, text=text)))
        self.assertEqual(self.client.calls, [])

    def test_old_prefix_does_not_reply(self):
        message = dict(self.message, text="!야구 시작")
        self.assertFalse(dispatch(self.client, self.bot, message))
        self.assertEqual(self.client.calls, [])

    def test_ignores_own_messages(self):
        self.assertFalse(dispatch(
            self.client, self.bot, dict(self.message, fromMe=True)
        ))
        self.assertEqual(self.client.calls, [])

    def test_ignores_non_text(self):
        for kind in ("photo", "feed", "file"):
            with self.subTest(kind=kind):
                self.assertFalse(dispatch(
                    self.client, self.bot, dict(self.message, kind=kind)
                ))

    def test_does_not_reply_to_unrelated_text(self):
        self.assertFalse(dispatch(
            self.client, self.bot, dict(self.message, text="안녕하세요")
        ))
        self.assertEqual(self.client.calls, [])

    def test_accepts_reply_messages(self):
        self.assertTrue(is_target_message(
            dict(self.message, kind="reply")
        ))

    def test_stream_parser(self):
        chunks = [
            b": ping\n",
            b"event: message\n",
            b'data: {"ok":true,"message":{"id":"1"}}\n',
            b"\n",
            b"event: cursor\n",
            b'data: {"ok":true,"cursor":{"100":"1"}}\n',
            b"\n",
        ]
        result = list(iter_sse(chunks))
        self.assertEqual([kind for kind, _ in result], ["message", "cursor"])
        self.assertEqual(result[0][1]["message"]["id"], "1")
        self.assertEqual(result[1][1]["cursor"]["100"], "1")

    def test_missing_room_id_is_ignored(self):
        self.assertFalse(dispatch(
            self.client, self.bot, dict(self.message, roomId=None)
        ))
        self.assertEqual(self.client.calls, [])

    def test_duplicate_room_titles_are_independent(self):
        first = dict(self.message, roomId="100", room="샘플방")
        second = dict(self.message, roomId="200", room="샘플방")
        self.assertTrue(dispatch(self.client, self.bot, first))
        self.assertTrue(dispatch(self.client, self.bot, second))
        self.assertEqual(set(self.bot.games), {"100:42", "200:42"})

    def test_other_room_title_is_allowed(self):
        message = dict(self.message, roomId="300", room="일반 채팅")
        self.assertTrue(dispatch(self.client, self.bot, message))
        self.assertEqual(self.client.calls[0][0], "300")


if __name__ == "__main__":
    unittest.main()

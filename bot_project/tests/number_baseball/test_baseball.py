import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "number-baseball-bot"))
from number_baseball_bot import BaseballBot, score


class BaseballTests(unittest.TestCase):
    def setUp(self):
        self.bot = BaseballBot(secret_factory=lambda: "427")

    def test_scoring(self):
        self.assertEqual(score("427", "427"), (3, 0))
        self.assertEqual(score("427", "274"), (0, 3))
        self.assertEqual(score("427", "429"), (2, 0))
        self.assertEqual(score("427", "715"), (0, 1))
        self.assertEqual(score("427", "135"), (0, 0))

    def test_ignores_unrelated_text(self):
        self.assertIsNone(self.bot.handle("a", "안녕"))
        self.assertIsNone(self.bot.handle("a", "!날씨"))
        self.assertIsNone(self.bot.handle("a", "!숫자야구시작"))

    def test_start_and_win(self):
        self.assertIn("시작", self.bot.handle("a", "!숫자야구 시작"))
        self.assertIn("정답", self.bot.handle("a", "!숫자야구 427"))
        self.assertNotIn("a", self.bot.games)

    def test_ball_and_strike(self):
        self.bot.handle("a", "!숫자야구 시작")
        first = self.bot.handle("a", "!숫자야구 274")
        self.assertIn("0S 3B", first)
        self.assertIn("직전 추측: !숫자야구 274", first)
        second = self.bot.handle("a", "!숫자야구 429")
        self.assertIn("2S 0B", second)
        self.assertIn("직전 추측: !숫자야구 429", second)
        self.assertIn("3번째", self.bot.handle("a", "!숫자야구 135"))

    def test_no_game(self):
        self.assertIn("먼저", self.bot.handle("a", "!숫자야구 123"))

    def test_invalid_guesses(self):
        self.bot.handle("a", "!숫자야구 시작")
        for guess in ("112", "012", "1234", "가나다", "11", "시도"):
            with self.subTest(guess=guess):
                self.assertIn("중복 없는", self.bot.handle("a", f"!숫자야구 {guess}"))
        self.assertEqual(self.bot.games["a"].attempts, 0)

    def test_give_up(self):
        self.bot.handle("a", "!숫자야구 시작")
        self.assertIn("427", self.bot.handle("a", "!숫자야구 포기"))
        self.assertIn("없습니다", self.bot.handle("a", "!숫자야구 포기"))

    def test_independent_per_user(self):
        self.bot.handle("a", "!숫자야구 시작")
        self.assertIn("먼저", self.bot.handle("b", "!숫자야구 123"))
        self.bot.handle("b", "!숫자야구 시작")
        self.bot.handle("a", "!숫자야구 포기")
        self.assertIn("정답", self.bot.handle("b", "!숫자야구 427"))

    def test_start_resets_game(self):
        self.bot.handle("a", "!숫자야구 시작")
        self.bot.handle("a", "!숫자야구 123")
        self.bot.handle("a", "!숫자야구 시작")
        self.assertIn("1번째", self.bot.handle("a", "!숫자야구 123"))

    def test_help_only_on_exact_command(self):
        reply = self.bot.handle("a", "!숫자야구도움말")
        self.assertTrue(reply.startswith("⚾ 숫자야구 | 도움말\n" + "\u200b" * 500))
        self.assertIn("!숫자야구 포기", reply)
        self.assertGreater(len(reply), 500)
        for command in ("!숫자야구", "!숫자야구 도움말", "!숫자야구도움말 추가"):
            with self.subTest(command=command):
                self.assertIsNone(self.bot.handle("a", command))
        self.assertEqual(self.bot.games, {})

    def test_old_commands_are_ignored(self):
        for command in ("!야구", "!야구 시작", "!야구 123",
                        "!야구 포기", "!야구 도움말", "!숫자야구시작"):
            with self.subTest(command=command):
                self.assertIsNone(self.bot.handle("a", command))
        self.assertEqual(self.bot.games, {})

    def test_replies_have_emojis_and_line_breaks(self):
        replies = [
            self.bot.handle("a", "!숫자야구 123"),
            self.bot.handle("a", "!숫자야구 시작"),
            self.bot.handle("a", "!숫자야구 112"),
            self.bot.handle("a", "!숫자야구 135"),
            self.bot.handle("a", "!숫자야구 429"),
            self.bot.handle("a", "!숫자야구 427"),
            self.bot.handle("a", "!숫자야구 포기"),
        ]
        for reply in replies:
            self.assertIn("\n\n", reply)
            self.assertTrue(any(icon in reply for icon in ("⚠️", "⚾", "🔎", "🎉", "ℹ️")))
            self.assertLess(len(reply), 500)

    def test_invalid_factory(self):
        bot = BaseballBot(secret_factory=lambda: "111")
        with self.assertRaises(ValueError):
            bot.handle("a", "!숫자야구 시작")


if __name__ == "__main__":
    unittest.main()

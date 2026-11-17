"""실제 발송 없이 명령 처리 경로를 확인한다."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api-base-summary-bot"))
import api_summary_bot as run


def msg(n, text):
    return {"id": str(n), "roomId": "100", "senderId": "42",
            "fromMe": False, "kind": "text", "text": text}


class Client:
    def __init__(self, unread):
        self.unread = unread
        self.sent = []

    def _request(self, route):
        return {"rooms": [{"id": "100", "unread": self.unread,
                            "lastMessageId": "3", "readable": True}]}

    def send(self, room, text):
        self.sent.append((room, text))
        return {"submitted": True}


class RunTests(unittest.TestCase):
    def test_missing_source_api_key_stops_before_starting_server(self):
        for value in ("YOURAPIKEY", "", "   "):
            with self.subTest(key=value), \
                 patch.object(run.sys, "argv", ["api_summary_bot.py"]), \
                 patch.object(run, "GEMINI_API_KEY", value), \
                 patch.object(run, "serve_process") as server:
                self.assertEqual(run.main(), 2)
                server.assert_not_called()

    def test_check_ai_skips_kakaotalk_and_checks_model(self):
        with patch.object(run.sys, "argv", ["run.py", "--check-ai"]), \
             patch.object(run, "GEMINI_API_KEY", "dummy-test-key"), \
             patch.object(run, "summarize", return_value="회의 확정") as ai, \
             patch.object(run, "serve_process") as server:
            self.assertEqual(run.main(), 0)
        ai.assert_called_once()
        server.assert_not_called()

    def test_next_free_port_avoids_existing_server(self):
        import socket

        # 실제 Windows TCP 바인딩을 사용하여 포트 충돌 회피 확인.
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            occupied = busy.getsockname()[1]
            if occupied >= 65535:
                self.skipTest("No higher TCP port available")
            alternate = run.next_free_port(occupied)
            self.assertNotEqual(alternate, occupied)
            with socket.socket() as free:
                free.bind(("127.0.0.1", alternate))

    def test_exact_other_account_command_only(self):
        self.assertTrue(run.is_command(msg(3, "!요약")))
        self.assertFalse(run.is_command(msg(3, "!요약 지금")))
        own = msg(3, "!요약")
        own["fromMe"] = True
        self.assertFalse(run.is_command(own))

    def test_help_command_exact_match(self):
        self.assertTrue(run.is_help_command(msg(3, "!요약도움말")))
        self.assertTrue(run.is_help_command(msg(3, " !요약도움말 ")))
        self.assertFalse(run.is_help_command(msg(3, "!요약도움말 지금")))
        self.assertFalse(run.is_command(msg(3, "!요약도움말")))
        own = msg(3, "!요약도움말")
        own["fromMe"] = True
        self.assertFalse(run.is_help_command(own))

    def test_help_reply_does_not_call_ai_or_history(self):
        client = Client(0)
        with patch.object(run, "summarize") as ai, \
             patch.object(run, "fetch_page") as history:
            run.handle_help_command(client, msg(3, "!요약도움말"))
        ai.assert_not_called()
        history.assert_not_called()
        self.assertEqual(client.sent[0][0], "100")
        self.assertIn("!요약", client.sent[0][1])
        self.assertIn("!요약도움말", client.sent[0][1])
        self.assertTrue(client.sent[0][1].startswith(
            "📖 AI 대화 요약 봇 도움말\n" + "\u200b" * 500 + "\n\n"
        ))
        for removed in (
            "처음 !요약은 요약 시작 위치만 등록합니다",
            "채팅방 읽음 여부와 관계없이",
            "요약 대상 대화는 외부 AI API로 전송됩니다",
        ):
            self.assertNotIn(removed, client.sent[0][1])

    def test_summary_event_dispatches_bang_summary(self):
        class EndLoop(Exception):
            pass

        client = Client(1)
        client.stream = lambda cursors: iter([
            ("message", {"ok": True, "message": msg(4, "!요약")})
        ])
        with patch.object(run, "CursorState") as cursor, \
             patch.object(run, "seed_cursors"), \
             patch.object(run.time, "sleep", side_effect=EndLoop), \
             patch.object(run, "handle_command") as handler:
            cursor.return_value.cursors = {}
            cursor.return_value.advance.return_value = True
            with self.assertRaises(EndLoop):
                run.run(client)
        handler.assert_called_once()
        self.assertEqual(handler.call_args.args[1]["text"], "!요약")

    def test_from_now_updates_existing_cursor(self):
        class TrackingState:
            cursors = {"100": "2"}
            advanced = []

            def advance(self, room_id, message_id):
                self.advanced.append((room_id, message_id))

        state = TrackingState()
        client = Client(1)
        run.seed_cursors(client, state, from_now=True)
        self.assertEqual(state.advanced, [("100", "3")])
        state.advanced.clear()
        run.seed_cursors(client, state)
        self.assertEqual(state.advanced, [])

    def test_help_event_is_routed_without_ai(self):
        class EndLoop(Exception):
            pass

        client = Client(0)
        client.stream = lambda cursors: iter([
            ("message", {"ok": True, "message": msg(4, "!요약도움말")})
        ])
        with patch.object(run, "CursorState") as cursor, \
             patch.object(run, "seed_cursors"), \
             patch.object(run.time, "sleep", side_effect=EndLoop), \
             patch.object(run, "summarize") as ai:
            cursor.return_value.cursors = {}
            cursor.return_value.advance.return_value = True
            with self.assertRaises(EndLoop):
                run.run(client)
        ai.assert_not_called()
        self.assertEqual(len(client.sent), 1)
        self.assertIn("도움말", client.sent[0][1])

    def test_summary_and_reply_with_zero_unread(self):
        import tempfile
        client = Client(0)
        messages = [msg(1, "!요약"), msg(2, "첫 대화"),
                    msg(3, "둘째 대화"), msg(4, "!요약")]
        def page(room, before_id, limit):
            return [m for m in messages if int(m["id"]) < before_id][-limit:]
        with tempfile.TemporaryDirectory() as tmp:
            boundaries = run.SummaryBoundaryState(Path(tmp) / "summary.json")
            with patch.object(run, "fetch_page", side_effect=page), \
                 patch.object(run, "summarize", return_value="회의 일정 확정") as ai:
                run.handle_command(client, messages[-1], boundaries)
            self.assertEqual(boundaries.get("100"), "4")
        self.assertEqual(len(client.sent), 1)
        self.assertIn("회의 일정 확정", client.sent[0][1])
        self.assertIn("📝 AI 대화 요약", client.sent[0][1])
        self.assertIn("📊 요약 범위", client.sent[0][1])
        self.assertIn("• 마지막 요약 이후 대화: 2개", client.sent[0][1])
        self.assertNotIn("• 읽음 여부와 무관한 메시지 ID 기준", client.sent[0][1])
        self.assertNotIn("ℹ️ 이번 !요약까지 기준점을 갱신했습니다.", client.sent[0][1])
        self.assertNotIn("직전 !요약 이후 대화:", client.sent[0][1])
        self.assertEqual(len(ai.call_args.args[0]), 2)

    def test_stale_room_key_status_does_not_block_readable_history(self):
        import tempfile
        client = Client(0)
        client._request = lambda route: {"rooms": [{"id": "100", "readable": False}]}
        messages = [msg(1, "!요약"), msg(2, "테스트 대화"), msg(3, "!요약")]

        def page(room, before_id, limit):
            return [m for m in messages if int(m["id"]) < before_id][-limit:]

        with tempfile.TemporaryDirectory() as tmp:
            boundaries = run.SummaryBoundaryState(Path(tmp) / "summary.json")
            with patch.object(run, "fetch_page", side_effect=page), \
                 patch.object(run, "summarize", return_value="테스트 성공"):
                run.handle_command(client, messages[-1], boundaries)
            self.assertEqual(boundaries.get("100"), "3")
        self.assertIn("테스트 성공", client.sent[0][1])

    def test_first_command_initializes_without_history(self):
        import tempfile
        client = Client(0)
        with tempfile.TemporaryDirectory() as tmp:
            boundaries = run.SummaryBoundaryState(Path(tmp) / "summary.json")
            with patch.object(run, "summarize") as ai, \
                 patch.object(run, "fetch_page", return_value=[]):
                run.handle_command(client, msg(3, "!요약"), boundaries)
            self.assertEqual(boundaries.get("100"), "3")
        ai.assert_not_called()
        self.assertIn("요약 시작점", client.sent[0][1])


if __name__ == "__main__":
    unittest.main()


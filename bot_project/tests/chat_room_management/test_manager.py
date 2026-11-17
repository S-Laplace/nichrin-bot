"""실제 카카오톡을 열지 않는 채팅방관리봇 단위 테스트."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "chat-room-management-bot"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chat_room_management_bot import HELP, VIEW_MORE, NichirinError, Cursor, Manager, Registry, parse_command


class FakeClient:
    def __init__(self):
        self.data = [
            {"id": "100", "title": "ADMINROOM", "readable": False, "lastMessageId": "5"},
            {"id": "200", "title": "친목방", "readable": False, "lastMessageId": "11"},
            {"id": "300", "title": "스터디방", "readable": False, "lastMessageId": "20"},
        ]
        self.open_windows = set()
        self.sends = []
        self.contexts = []
        self.dry_runs = []
        self.fail_id = None
        self.fail_first_id = None
        self.missing_input_ids = set()
        self.dry_run_error_ids = set()

    def _request(self, path, body=None):
        if path == "/rooms":
            return {"ok": True, "rooms": copy.deepcopy(self.data)}
        if path.startswith("/context?"):
            query = parse_qs(urlsplit(path).query)
            rid = query["roomId"][0]
            self.contexts.append(query)
            if rid == self.fail_id:
                raise NichirinError("HTTP 503 [KEY_UNAVAILABLE]")
            if rid == self.fail_first_id:
                self.fail_first_id = None
                room = next(r for r in self.data if r["id"] == rid)
                room["readable"] = True
                self.open_windows.add(rid)
                raise NichirinError("HTTP 503 [KEY_UNAVAILABLE]")
            room = next(r for r in self.data if r["id"] == rid)
            room["readable"] = True
            self.open_windows.add(rid)
            return {"ok": True, "messages": []}
        if path == "/send" and body is not None:
            assert body["dryRun"] is True
            rid = body["room"]
            self.dry_runs.append(dict(body))
            if body["openRoom"]:
                self.open_windows.add(rid)
            if rid in self.dry_run_error_ids:
                raise NichirinError("HTTP 503 [BUSY] send check failed")
            if rid not in self.open_windows:
                raise NichirinError("WINDOW_NOT_FOUND")
            if rid in self.missing_input_ids:
                raise NichirinError("HTTP 503 [INPUT_NOT_FOUND]")
            return {"ok": True, "submitted": False, "dryRun": True}
        raise AssertionError(f"unexpected API: {path}")

    def send(self, room_id, text):
        self.sends.append((room_id, text))
        return {"ok": True, "submitted": True}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.client = FakeClient()
        self.registry = Registry(self.base / "rooms.json")
        self.manager = Manager(self.client, self.registry)
        self.manager.admin_id = "100"

    def msg(self, room_id="100", from_me=False, text="!채팅방관리도움말"):
        return {"id": "24", "roomId": room_id, "senderId": "99",
                "fromMe": from_me, "kind": "text", "text": text}

    def test_startup_activates_admin_first_and_only_sends_to_admin(self):
        self.manager.activate_admin()
        self.assertEqual(self.client.sends, [("100", "nichirin bot이 활성화되었습니다.")])
        self.assertEqual(self.client.contexts[0]["roomId"], ["100"])
        self.assertTrue(self.client.dry_runs[0]["dryRun"])
        self.assertIn("🔑 읽기: ✅ 가능", self.manager.check_status(self.client.data[0]).lines())
        self.assertIn("⌨️ 전송 입력창: ✅ 확인됨", self.manager.check_status(self.client.data[0]).lines())

    def test_only_admin_room_and_others_messages(self):
        self.assertIsNone(self.manager.answer(self.msg(room_id="200")))
        self.assertIsNone(self.manager.answer(self.msg(from_me=True)))
        bad = self.msg()
        bad["senderId"] = None
        self.assertIsNone(self.manager.answer(bad))
        self.assertIn("도움말", self.manager.answer(self.msg()))

    def test_help_is_collapsed_and_only_one_command_works(self):
        self.assertEqual(VIEW_MORE, "\u200b" * 500)
        self.assertTrue(HELP.startswith("📖 채팅방 관리봇 도움말\n" + VIEW_MORE + "\n\n"))
        self.assertEqual(HELP.count("!채팅방관리도움말"), 1)
        for command in ("!방활성화", "!방등록", "!방삭제", "!방목록",
                        "!방전체활성화", "!방상태", "!채팅방관리도움말"):
            self.assertIn(command, HELP)
        for extra in ("명령은 관리자", "방을 열면", "dryRun", "다른 봇 프로세스"):
            self.assertNotIn(extra, HELP)
        self.assertEqual(self.manager.answer(self.msg()), HELP)
        for old in ("!방도움말", "!방관리", "!방", "!채팅방도움말",
                    "!채팅방관리", "!채팅방관리도움말 추가"):
            self.assertIsNone(self.manager.answer(self.msg(text=old)), old)

    def test_response_layout_for_management_commands(self):
        registered = self.manager.answer(self.msg(text="!방등록 친목방"))
        self.assertTrue(registered.startswith("✅ 채팅방 등록 완료\n\n📌"))
        listed = self.manager.answer(self.msg(text="!방목록"))
        self.assertTrue(listed.startswith("📋 관리 채팅방 목록\n📌"))
        self.assertIn("📍 친목방", listed)
        status = self.manager.answer(self.msg(text="!방상태 친목방"))
        self.assertTrue(status.startswith("🔎 채팅방 상태\n\n📌"))
        deleted = self.manager.answer(self.msg(text="!방삭제 친목방"))
        self.assertTrue(deleted.startswith("🗑️ 채팅방 등록 삭제 완료\n\n📌"))
        error = self.manager.answer(self.msg(text="!방상태 없는방"))
        self.assertTrue(error.startswith("⚠️ 채팅방 상태 실패\n\n🔎"))
        self.assertIn("!채팅방관리도움말", error)

    def test_register_activates_and_persists_id(self):
        reply = self.manager.answer(self.msg(text="!방등록 친목방"))
        self.assertIn("🔄 활성화: ✅ 확인", reply)
        self.assertIn("🔑 읽기: ✅ 가능", reply)
        self.assertIn("⌨️ 전송 입력창: ✅ 확인됨", reply)
        self.assertEqual(self.registry.rooms, {"200": "친목방"})
        self.assertEqual(json.loads((self.base / "rooms.json").read_text(encoding="utf-8")),
                         {"rooms": {"200": "친목방"}})
        self.assertEqual(self.client.sends, [])

    def test_delete_does_not_close_room(self):
        self.registry.register("200", "친목방")
        self.manager.answer(self.msg(text="!방삭제 친목방"))
        self.assertEqual(self.registry.rooms, {})
        self.assertFalse(self.client.contexts)

    def test_bulk_activates_only_inactive_registered_rooms(self):
        self.registry.register("200", "친목방")
        self.registry.register("300", "스터디방")
        self.client.data[1]["readable"] = True
        self.client.open_windows.add("200")
        result = self.manager.answer(self.msg(text="!방전체활성화"))
        self.assertIn("🔄 채팅방 일괄 활성화 결과", result)
        self.assertIn("📍 스터디방 — ✅ 활성화 확인", result)
        self.assertIn("📍 친목방 — ℹ️ 이미 준비됨", result)
        self.assertEqual([x["roomId"][0] for x in self.client.contexts], ["300"])
        self.assertEqual(self.client.sends, [])

    def test_bulk_retries_key_read_without_reopening_chat(self):
        self.registry.register("200", "친목방")
        self.client.fail_first_id = "200"
        with patch("chat_room_management_bot.time.sleep", return_value=None) as sleep:
            response = self.manager.answer(self.msg(text="!방전체활성화"))
        self.assertIn("✅ 새로 활성화: 1개", response)
        self.assertEqual([entry["open"] for entry in self.client.contexts],
                         [["true"], ["false"]])
        sleep.assert_called_once_with(0.5)
        self.assertEqual([x["openRoom"] for x in self.client.dry_runs], [False, True, False])
        self.assertEqual(self.client.sends, [])

    def test_bulk_key_failure_reports_korean_reason(self):
        self.registry.register("200", "친목방")
        self.client.fail_id = "200"
        with patch("chat_room_management_bot.time.sleep", return_value=None):
            response = self.manager.answer(self.msg(text="!방전체활성화"))
        self.assertIn("활성화 미완료: 1개", response)
        self.assertIn("복호화 키를 확보하지 못했습니다", response)
        self.assertNotIn("Service Unavailable", response)
        self.assertEqual(len(self.client.contexts), 4)

    def test_failed_activation_keeps_registration_and_reports_failure(self):
        self.client.fail_id = "200"
        with patch("chat_room_management_bot.time.sleep", return_value=None):
            result = self.manager.answer(self.msg(text="!방등록 친목방"))
        self.assertIn("🔄 활성화: ⚠️ 미완료", result)
        self.assertIn("200", self.registry.rooms)
        self.assertEqual(self.client.sends, [])

    def test_list_does_not_open_missing_room(self):
        self.registry.register("200", "친목방")
        result = self.manager.answer(self.msg(text="!방목록"))
        self.assertIn("🔑 읽기: ❌ 불가 (복호화 키 미확보)", result)
        self.assertIn("⌨️ 전송 입력창: ⚠️ 미확인 (채팅방 창 없음)", result)
        self.assertNotIn("ID 200", result)
        self.assertEqual(self.client.contexts, [])
        self.assertEqual(self.client.sends, [])

    def test_readable_but_sender_window_closed_is_not_green(self):
        self.client.data[1]["readable"] = True
        result = self.manager.answer(self.msg(text="!방상태 친목방"))
        self.assertIn("🔑 읽기: ✅ 가능", result)
        self.assertIn("⌨️ 전송 입력창: ⚠️ 미확인 (채팅방 창 없음)", result)

    def test_reading_unavailable_but_input_visible(self):
        self.client.open_windows.add("200")
        status = self.manager.answer(self.msg(text="!방상태 친목방"))
        self.assertIn("🔑 읽기: ❌ 불가", status)
        self.assertIn("⌨️ 전송 입력창: ✅ 확인됨", status)
        self.assertEqual(self.client.contexts, [])
        self.assertFalse(self.client.dry_runs[-1]["openRoom"])
        self.assertEqual(self.client.sends, [])

    def test_reading_available_but_input_missing(self):
        self.client.data[1]["readable"] = True
        self.client.open_windows.add("200")
        self.client.missing_input_ids.add("200")
        status = self.manager.answer(self.msg(text="!방상태 친목방"))
        self.assertIn("🔑 읽기: ✅ 가능", status)
        self.assertIn("⌨️ 전송 입력창: ⚠️ 미확인 (입력창을 찾지 못함)", status)

    def test_input_check_error_is_not_reported_as_available(self):
        self.client.data[1]["readable"] = True
        self.client.dry_run_error_ids.add("200")
        result = self.manager.answer(self.msg(text="!방상태 친목방"))
        self.assertIn("🔑 읽기: ✅ 가능", result)
        self.assertIn("⌨️ 전송 입력창: ⚠️ 미확인 (시험 점검 실패)", result)

    def test_missing_registered_room_masks_numeric_fallback_name(self):
        self.registry.register("987654321", "987654321")
        result = self.manager.answer(self.msg(text="!방목록"))
        self.assertIn("📍 이름 미확인 #1", result)
        self.assertNotIn("987654321", result)
        self.assertNotIn("(ID ", result)
        self.assertIn("🔑 읽기: ⚪ 확인 불가", result)
        self.assertIn("⌨️ 전송 입력창: ⚠️ 미확인 (채팅방 목록에 없음)", result)

    def test_activate_success_reports_both_checks(self):
        result = self.manager.answer(self.msg(text="!방활성화 친목방"))
        self.assertIn("✅ 채팅방 활성화 확인", result)
        self.assertIn("🔑 읽기: ✅ 가능", result)
        self.assertIn("⌨️ 전송 입력창: ✅ 확인됨", result)
        self.assertIn("실제 메시지 전달을 보증하지 않습니다", result)
        self.assertEqual(self.client.sends, [])

    def test_registered_room_appears_without_id(self):
        self.registry.register("200", "친목방")
        self.client.open_windows.add("200")
        result = self.manager.answer(self.msg(text="!방목록"))
        self.assertIn("📍 친목방\n🔑 읽기: ❌ 불가", result)
        self.assertIn("⌨️ 전송 입력창: ✅ 확인됨", result)
        self.assertNotIn("(ID ", result)
        self.assertNotIn("ID 200", result)

    def test_delete_and_repeat_delete_are_distinct(self):
        self.registry.register("200", "친목방")
        deleted = self.manager.answer(self.msg(text="!방삭제 친목방"))
        self.assertIn("🗂️ 관리 목록: 삭제됨", deleted)
        self.assertIn("🔑 읽기: ❌ 불가", deleted)
        self.assertIn("⌨️ 전송 입력창: ⚠️ 미확인", deleted)
        failed = self.manager.answer(self.msg(text="!방삭제 친목방"))
        self.assertIn("⚠️ 채팅방 삭제 실패", failed)
        self.assertIn("🗂️ 관리 목록: 변경 없음", failed)

    def test_activate_failure_keeps_independent_input_status(self):
        self.client.open_windows.add("200")
        self.client.fail_id = "200"
        with patch("chat_room_management_bot.time.sleep", return_value=None):
            result = self.manager.answer(self.msg(text="!방활성화 친목방"))
        self.assertIn("⚠️ 채팅방 활성화 미완료", result)
        self.assertIn("🔑 읽기: ❌ 불가", result)
        self.assertIn("⌨️ 전송 입력창: ✅ 확인됨", result)
        self.assertIn("복호화 키를 확보하지 못했습니다", result)

    def test_bulk_shows_mixed_results_by_room(self):
        self.registry.register("200", "친목방")
        self.registry.register("300", "스터디방")
        self.client.open_windows.add("200")
        self.client.fail_id = "200"
        with patch("chat_room_management_bot.time.sleep", return_value=None):
            result = self.manager.answer(self.msg(text="!방전체활성화"))
        self.assertIn("✅ 새로 활성화: 1개", result)
        self.assertIn("⚠️ 활성화 미완료: 1개", result)
        self.assertIn("📍 친목방 — ⚠️ 활성화 미완료\n🔑 읽기: ❌ 불가", result)
        self.assertIn("⌨️ 전송 입력창: ✅ 확인됨", result)
        self.assertIn("📍 스터디방 — ✅ 활성화 확인\n🔑 읽기: ✅ 가능", result)

    def test_duplicate_title_rejected(self):
        self.client.data.append({"id": "900", "title": "친목방", "readable": False})
        result = self.manager.answer(self.msg(text="!방활성화 친목방"))
        self.assertIn("여러 개", result)
        self.assertFalse(self.client.contexts)


class ParseAndCursorTests(unittest.TestCase):
    def test_parse_commands(self):
        self.assertEqual(parse_command("!채팅방 등록 친목방"), ("등록", "친목방"))
        self.assertEqual(parse_command("!방전체활성화"), ("전체활성화", ""))
        self.assertEqual(parse_command("!채팅방관리도움말"), ("도움말", ""))
        for alias in ("!방", "!방관리", "!방도움말", "!채팅방도움말",
                      "!채팅방관리", "!방 도움말", "!채팅방 도움말",
                      "!채팅방관리도움말 추가"):
            self.assertIsNone(parse_command(alias), alias)
        self.assertIsNone(parse_command("!요약"))

    def test_cursor_is_atomic_and_prevents_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cursor.json"
            state = Cursor(path)
            state.advance("100", "10")
            self.assertFalse(state.advance("100", "9"))
            self.assertTrue(state.advance("100", "11"))
            self.assertFalse(Cursor(path).advance("100", "11"))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"100": "11"})


if __name__ == "__main__":
    unittest.main()

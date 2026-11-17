"""Visible reservation numbers are compact; SQLite IDs and delivery logs stay stable."""
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from reserved_message_bot import SchedulerDB, dispatch_once
from reserved_message_bot import answer


class NumberingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = SchedulerDB(Path(self.folder.name) / "jobs.sqlite3")
        self.now = datetime(2030, 1, 10, 10, 0)

    def tearDown(self):
        self.folder.cleanup()

    def command(self, text, room="100", owner="50"):
        return answer(self.db, text, room, owner, "테스트방", now=self.now)

    def numbers(self, room="100", owner="50"):
        return re.findall(r"📨 #(\d+)", self.command("!예약 목록", room, owner))
 
    def test_completed_once_compacts_numbers_and_keeps_audit(self):
        a = self.command("!예약 추가 2030-01-10 10:01 1회 첫 번째")
        b = self.command("!예약 추가 2030-01-10 10:02 매일 두 번째")
        c = self.command("!예약 추가 2030-01-10 10:03 매주 세 번째")
        self.assertIn("#1", a)
        self.assertIn("#2", b)
        self.assertIn("#3", c)
        ids = [row["id"] for row in self.db.list_active_owned("100", "50")]
        self.assertEqual(self.numbers(), ["1", "2", "3"])
        sends = []
        due = self.now + timedelta(minutes=1)
        self.assertTrue(dispatch_once(
            self.db, lambda room, msg: sends.append((room, msg)) or {"submitted": True},
            now=due))
        self.assertEqual(sends, [("100", "[📅 예약 메시지]\n첫 번째")])
        self.assertEqual(self.db.history()[0]["job_id"], ids[0])
        self.assertEqual(self.db.history()[0]["status"], "submitted")
        self.assertEqual(self.numbers(), ["1", "2"])
        self.assertEqual(
            [row["id"] for row in self.db.list_active_owned("100", "50")], ids[1:])

        self.assertIn("예약 #1 삭제 완료", self.command("!예약 삭제 1"))
        self.assertEqual(self.numbers(), ["1"])
        self.assertEqual(
            [row["id"] for row in self.db.list_active_owned("100", "50")], [ids[2]])
        self.assertIn("세 번째", self.command("!예약 목록"))
        self.assertNotIn("두 번째", self.command("!예약 목록"))
        self.assertEqual(self.db.history()[0]["job_id"], ids[0])
        self.assertEqual(len(self.db.list_jobs("100", "50")), 3)

    def test_other_owners_and_rooms_have_separate_numbering(self):
        self.command("!예약 추가 2030-01-10 10:01 1회 A1", "100", "50")
        self.command("!예약 추가 2030-01-10 10:02 매일 A2", "100", "50")
        self.command("!예약 추가 2030-01-10 10:03 매주 B1", "100", "60")
        self.command("!예약 추가 2030-01-10 10:04 매월 C1", "200", "50")
        self.assertEqual(self.numbers("100", "50"), ["1", "2"])
        self.assertEqual(self.numbers("100", "60"), ["1"])
        self.assertEqual(self.numbers("200", "50"), ["1"])
        self.assertIn("삭제 완료", self.command("!예약 삭제 1", "100", "50"))
        self.assertEqual(self.numbers("100", "50"), ["1"])
        self.assertEqual(self.numbers("100", "60"), ["1"])
        self.assertEqual(self.numbers("200", "50"), ["1"])

    def test_creation_order_controls_display_and_deletion(self):
        first = self.command("!예약 추가 2030-01-12 10:00 1회 테스트1")
        second = self.command("!예약 추가 2030-01-11 10:00 1회 테스트2")
        self.assertIn("예약 등록 완료 · #1", first)
        self.assertIn("예약 등록 완료 · #2", second)
        self.assertIn("📌 [테스트방] 채팅방에 전송", second)
        listed = self.command("!예약 목록")
        self.assertIn("📋 테스트방 예약 목록", listed)
        self.assertLess(listed.index("테스트1"), listed.index("테스트2"))
        self.assertEqual(self.numbers(), ["1", "2"])
        self.assertIn("예약 #1 삭제 완료", self.command("!예약 삭제 1"))
        self.assertNotIn("테스트1", self.command("!예약 목록"))
        self.assertIn("테스트2", self.command("!예약 목록"))
        self.assertEqual(self.numbers(), ["1"])

    def test_help_uses_zero_width_view_more_and_empty_room_title(self):
        help_text = self.command("!예약도움말")
        self.assertTrue(help_text.startswith("📅 예약 발송 봇 도움말\n" + "\u200b" * 500))
        self.assertIn("!예약 추가", help_text)
        self.assertIn("📭 테스트방 예약 목록", self.command("!예약 목록"))

    def test_out_of_range_number_and_repeating_job(self):
        self.command("!예약 추가 2030-01-10 10:01 매일 반복")
        self.assertIn("삭제할 수 없는", self.command("!예약 삭제 999"))
        self.assertEqual(self.numbers(), ["1"])
        dispatch_once(self.db, lambda *_: {"submitted": True},
                      now=self.now + timedelta(minutes=1))
        self.assertEqual(self.numbers(), ["1"])
        self.assertIn("반복", self.command("!예약 목록"))
        self.assertEqual(self.db.history()[0]["status"], "submitted")


if __name__ == "__main__":
    unittest.main()

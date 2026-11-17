"""Reservation database, room authorization, chat commands and admin HTTP tests."""
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from reserved_message_bot import SchedulerDB, dispatch_once, next_time
from reserved_message_bot import make_handler
from reserved_message_bot import answer, handle_message, run_chat_bot, verified_room_titles
from reserved_message_bot import update_pending_titles


class FakeClient:
    def __init__(self):
        self.rooms = [{"id": "123", "title": "업무방"},
                      {"id": "456", "title": "안내방"}]

    def _request(self, path):
        if path != "/rooms":
            raise RuntimeError("wrong path")
        return {"rooms": self.rooms}


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SchedulerDB(Path(self.tmp.name) / "test.sqlite3")
        self.now = datetime(2030, 1, 10, 10, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, repeat="once", at="2030-01-10T10:01", room="123", owner=""):
        return self.db.add(room, "업무방" if room == "123" else "안내방",
                           "공지 메시지", at, repeat, now=self.now,
                           owner_sender_id=owner)

    def command(self, text, room="123", sender="101"):
        return answer(self.db, text, room, sender, room_title="업무방", now=self.now)

    def test_once_sent_and_never_again(self):
        self.add()
        sent = []
        clock = self.now + timedelta(minutes=1)
        self.assertTrue(dispatch_once(self.db, lambda room, msg:
                                      sent.append((room, msg)) or {"submitted": True},
                                      now=clock))
        self.assertFalse(dispatch_once(self.db, lambda *args: None, now=clock))
        self.assertEqual(sent, [("123", "[📅 예약 메시지]\n공지 메시지")])
        self.assertEqual(self.db.history()[0]["status"], "submitted")
        self.assertFalse(self.db.list_jobs()[0]["active"])

    def test_recurring_delivery_header_keeps_original_message_in_db(self):
        message = "첫 번째 줄\n두 번째 줄"
        self.db.add("123", "업무방", message, "2030-01-10T10:01",
                    "daily", now=self.now)
        sent = []
        dispatch_once(self.db, lambda room, body: sent.append((room, body)),
                      now=self.now + timedelta(minutes=1))
        self.assertEqual(sent, [("123", "[📅 예약 메시지]\n" + message)])
        job = self.db.list_jobs()[0]
        self.assertEqual(job["message"], message)
        self.assertEqual(job["next_run"], "2030-01-11T10:01:00")

    def test_recurring_daily_weekly_monthly(self):
        for kind, expected in [("daily", "2030-01-11T10:01:00"),
                               ("weekly", "2030-01-17T10:01:00"),
                               ("monthly", "2030-02-10T10:01:00")]:
            with self.subTest(kind=kind):
                isolated = SchedulerDB(Path(self.tmp.name) / (kind + ".sqlite3"))
                isolated.add("123", "업무방", "공지", "2030-01-10T10:01",
                             kind, now=self.now)
                isolated.claim_due(self.now + timedelta(minutes=1))
                self.assertEqual(isolated.list_jobs()[0]["next_run"], expected)

    def test_month_31_anchor_and_february(self):
        first = datetime(2028, 1, 31, 9)
        feb = next_time(first, "monthly", 31, first)
        self.assertEqual(feb, datetime(2028, 2, 29, 9))
        self.assertEqual(next_time(feb, "monthly", 31, feb), datetime(2028, 3, 31, 9))

    def test_missed_not_sent(self):
        self.add()
        calls = []
        dispatch_once(self.db, lambda *_: calls.append(1),
                      now=self.now + timedelta(minutes=10))
        self.assertEqual(calls, [])
        self.assertEqual(self.db.history()[0]["status"], "missed")

    def test_uncertain_never_automatically_retried(self):
        self.add()
        def fail(*_):
            raise RuntimeError("timeout")
        dispatch_once(self.db, fail, now=self.now + timedelta(minutes=1))
        self.assertEqual(self.db.history()[0]["status"], "uncertain")
        self.assertFalse(dispatch_once(self.db, fail, now=self.now + timedelta(minutes=2)))

    def test_disable_and_reload(self):
        job = self.add()
        self.assertTrue(self.db.set_active(job, False))
        self.assertIsNone(self.db.claim_due(self.now + timedelta(minutes=1)))
        self.assertFalse(SchedulerDB(self.db.path).list_jobs()[0]["active"])
        self.assertTrue(self.db.set_active(job, True))

    def test_validation(self):
        for at, kind, room in [("2029-01-01T00:00", "once", "123"),
                               ("2030-01-11T12:00", "hourly", "123"),
                               ("2030-01-11T12:00", "once", "room")]:
            with self.assertRaises(ValueError):
                self.db.add(room, "업무방", "hi", at, kind, now=self.now)

    def test_legacy_schema_migration(self):
        old_path = Path(self.tmp.name) / "legacy.sqlite3"
        with sqlite3.connect(old_path) as con:
            con.execute("""CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL, room_title TEXT NOT NULL,
                message TEXT NOT NULL, next_run TEXT, repeat TEXT NOT NULL,
                anchor_day INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL)""")
            con.execute("""INSERT INTO jobs(room_id,room_title,message,next_run,repeat,anchor_day,created_at)
                        VALUES('123','기존방','예전 예약','2030-01-20T10:00:00','once',20,'2030-01-01')""")
        con.close()
        migrated = SchedulerDB(old_path)
        self.assertEqual(migrated.list_jobs()[0]["owner_sender_id"], "")
        self.assertFalse(migrated.cancel_owned_position(1, "123", "101"))
        self.assertEqual(len(migrated.list_jobs()), 1)

    def test_repair_placeholder_title_without_changing_reservation(self):
        wrong = self.db.add("123", "123", "공지", "2030-01-10T10:01",
                            "daily", now=self.now)
        correct = self.db.add("456", "안내방", "안내", "2030-01-10T10:02",
                              "once", now=self.now)
        self.assertEqual(self.db.repair_room_titles(
            {"123": "업무방", "456": "456"}), 1)
        saved = {job["id"]: job for job in SchedulerDB(self.db.path).list_jobs()}
        self.assertEqual(saved[wrong]["room_title"], "업무방")
        self.assertEqual(saved[wrong]["message"], "공지")
        self.assertEqual(saved[wrong]["next_run"], "2030-01-10T10:01:00")
        self.assertEqual(saved[correct]["room_title"], "안내방")

    def test_missing_room_title_is_saved_then_repaired(self):
        for room_title in (None, "123"):
            message = "공지" if room_title is None else "후속 공지"
            reply = answer(self.db, f"!예약 추가 2030-01-10 10:01 1회 {message}",
                           "123", "101", room_title=room_title, now=self.now)
            self.assertIn("예약 등록 완료", reply)
            self.assertIn("이름 확인 후 자동으로 갱신", reply)
        before = self.db.list_jobs("123")
        self.assertEqual([job["room_title"] for job in before], ["123", "123"])
        self.assertEqual(self.db.repair_room_titles({"123": "업무방"}), 2)
        after = self.db.list_jobs("123")
        self.assertEqual([job["room_title"] for job in after], ["업무방", "업무방"])
        self.assertEqual([(j["id"], j["message"], j["next_run"]) for j in before],
                         [(j["id"], j["message"], j["next_run"]) for j in after])
        self.assertEqual(verified_room_titles([
            {"id": "123", "title": "123"},
            {"id": "456", "title": "안내방"},
        ]), {"456": "안내방"})

    def test_periodic_refresh_recovers_saved_name(self):
        self.db.add("123", "123", "공지", "2030-01-10T10:01", "daily",
                    now=self.now)
        client = FakeClient()
        class StopAfterOneScan:
            def __init__(self):
                self.calls = 0
            def wait(self, timeout):
                self.calls += 1
                self.timeout = timeout
                return self.calls > 1
        stop = StopAfterOneScan()
        update_pending_titles(client, self.db, stop)
        self.assertEqual(stop.timeout, 30)
        self.assertEqual(self.db.list_jobs("123")[0]["room_title"], "업무방")

    def test_refresh_does_not_query_rooms_without_pending_names(self):
        class NoRoomLookup:
            def _request(self, path):
                raise AssertionError("No room query expected")
        class StopAfterOneScan:
            calls = 0
            def wait(self, timeout):
                self.calls += 1
                return self.calls > 1
        update_pending_titles(NoRoomLookup(), self.db, StopAfterOneScan())

    def test_help_aliases_and_unrelated_text(self):
        for cmd in ("!예약", "!예약도움말", "!예약 도움말", "!예약발송도움말"):
            with self.subTest(cmd=cmd):
                self.assertIn("!예약 추가", self.command(cmd))
                self.assertIn("!예약 삭제", self.command(cmd))
        self.assertIsNone(self.command("안녕하세요"))
        self.assertIsNone(answer(self.db, "!예약", "invalid", "101"))

    def test_room_and_sender_isolation_for_creation_and_lists(self):
        a = self.command("!예약 추가 2030-01-10 10:01 1회 A에만 보내기")
        b = self.command("!예약 추가 2030-01-10 10:02 매일 B에만 보내기",
                         room="456", sender="101")
        c = self.command("!예약 추가 2030-01-10 10:03 매주 타인 예약",
                         room="123", sender="202")
        self.assertIn("예약 등록 완료", a)
        self.assertIn("매일", b)
        self.assertIn("매주", c)
        list_a = self.command("!예약 목록", "123", "101")
        self.assertIn("A에만 보내기", list_a)
        self.assertNotIn("B에만 보내기", list_a)
        self.assertNotIn("타인 예약", list_a)
        self.assertIn("B에만 보내기", self.command("!예약 목록", "456", "101"))
        self.assertIn("타인 예약", self.command("!예약 목록", "123", "202"))
        self.assertEqual(len(self.db.list_jobs("123", "101")), 1)
        self.assertEqual(len(self.db.list_jobs("456", "101")), 1)

    def test_delete_cannot_touch_another_room_or_sender(self):
        job = self.add("daily", owner="101")
        self.assertIn("삭제할 수 없는", self.command(f"!예약 삭제 {job}", room="456"))
        self.assertIn("삭제할 수 없는", self.command(f"!예약 삭제 {job}", sender="202"))
        self.assertTrue(self.db.list_jobs("123", "101")[0]["active"])
        self.assertIn("삭제 완료", self.command(f"!예약 삭제 {job}"))
        self.assertIn("활성 예약이 없습니다", self.command("!예약 목록"))
        self.assertIsNone(self.db.claim_due(self.now + timedelta(minutes=1)))

    def test_legacy_web_job_is_not_a_chat_users_job(self):
        self.add()
        self.assertIn("활성 예약이 없습니다", self.command("!예약 목록"))
        self.assertIn("삭제할 수 없는", self.command("!예약 삭제 1"))

    def test_chat_messages_send_only_to_originating_room(self):
        self.command("!예약 추가 2030-01-10 10:01 1회 업무 공지", room="123")
        self.command("!예약 추가 2030-01-10 10:01 1회 안내 공지", room="456")
        calls = []
        clock = self.now + timedelta(minutes=1)
        while dispatch_once(self.db, lambda room, msg: calls.append((room,msg)),
                            now=clock):
            pass
        self.assertEqual(set(calls), {("123", "[📅 예약 메시지]\n업무 공지"),
                                      ("456", "[📅 예약 메시지]\n안내 공지")})

    def test_bad_add_format_dates_and_delete(self):
        self.assertIn("형식", self.command("!예약 추가 2030-01-10"))
        self.assertIn("반복 주기", self.command("!예약 추가 2030-01-10 10:01 매시간 안녕"))
        self.assertIn("예약 등록 실패", self.command("!예약 추가 2029-01-01 10:00 1회 과거"))
        self.assertIn("예약 등록 실패", self.command("!예약 추가 2030-02-30 10:01 1회 잘못된날짜"))
        self.assertIn("예약 번호", self.command("!예약 삭제"))
        self.assertIn("도움말", self.command("!예약 방 123"))

    def test_sse_message_filters_and_duplicate_log_ids(self):
        cursor, names, output = {}, {"123": "업무방"}, []
        send = lambda room, reply: output.append((room, reply))
        msg = dict(roomId="123", id="10", senderId="101",
                   fromMe=False, kind="text", text="!예약 목록")
        self.assertIn("활성 예약이 없습니다",
                      handle_message(self.db, msg, names, cursor, sender=send))
        self.assertIsNone(handle_message(self.db, msg, names, cursor, sender=send))
        self.assertEqual(len(output), 1)
        for idx, change in enumerate([
            {"fromMe": True}, {"kind": "image"}, {"senderId": None},
            {"roomId": "oops"}, {"text": "일반 채팅"}], 11):
            copy = {**msg, **change, "id": str(idx)}
            self.assertIsNone(handle_message(self.db, copy, names, cursor, sender=send))
        self.assertEqual(len(self.db.list_jobs()), 0)

    def test_list_is_bounded_and_shows_message(self):
        for _ in range(20):
            self.add("daily", owner="101")
        reply = self.command("!예약 목록")
        self.assertIn("총 20건", reply)
        self.assertIn("공지 메시지", reply)
        self.assertIn("나머지", reply)
        self.assertLessEqual(len(reply), 1600)

    def test_stream_seeds_all_rooms_including_unreadable(self):
        stop = threading.Event()
        observed = []
        class WatchClient:
            def _request(self, route):
                return {"rooms": [
                    {"id": "123", "title": "기존방", "readable": True, "lastMessageId": "15"},
                    {"id": "456", "title": "새로 읽을 방", "readable": False, "lastMessageId": "27"},
                ]}
            def stream(self, cursor):
                observed.append(dict(cursor))
                stop.set()
                yield "ready", {"ok": True}
        run_chat_bot(WatchClient(), self.db, stop)
        self.assertEqual(observed, [{"123": "15", "456": "27"}])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SchedulerDB(Path(self.tmp.name) / "api.sqlite3")
        self.client = FakeClient()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          make_handler(self.db, self.client, False))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def fetch(self, path, method="GET", payload=None, headers=None):
        headers = {"Content-Type": "application/json", **(headers or {})}
        req = Request(self.url + path,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      method=method, headers=headers)
        with urlopen(req, timeout=5) as r:
            return r.status, json.load(r)

    def test_api_available_without_token(self):
        status, data = self.fetch("/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(data["live"])
        self.assertEqual(self.fetch("/api/jobs")[0], 200)
        self.assertEqual(self.fetch("/api/history")[0], 200)
        self.assertEqual(self.fetch("/api/rooms")[0], 200)

    def test_external_origin_and_host_are_rejected(self):
        for headers in (
            {"Host": "attacker.example"},
            {"Origin": "https://attacker.example"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                with self.assertRaises(HTTPError) as ctx:
                    self.fetch("/api/jobs", "POST", {},
                               headers=headers)
                self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(self.fetch("/api/jobs")[1]["jobs"], [])

    def test_local_page_static_files_are_served(self):
        files = {
            "/": (b"<html", "text/html"),
            "/assets/styles.css": (b".sidebar", "text/css"),
            "/assets/app.js": (b"function renderJobs", "text/javascript"),
        }
        for path, (expected, mime_type) in files.items():
            with self.subTest(path=path):
                with urlopen(self.url + path, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.headers["Content-Type"].startswith(mime_type))
                    self.assertIn(expected, response.read())


    def test_create_and_filter(self):
        code, result = self.fetch("/api/jobs", "POST",
                                  {"room_id": "123", "at": "2035-01-01T10:00",
                                   "repeat": "daily", "message": "공지"})
        self.assertEqual(code, 201)
        self.assertEqual(len(self.fetch("/api/jobs?room=123")[1]["jobs"]), 1)
        self.assertEqual(len(self.fetch("/api/jobs?room=456")[1]["jobs"]), 0)
        self.fetch("/api/jobs/" + str(result["id"]) + "/disable", "POST", {})
        self.assertFalse(self.fetch("/api/jobs")[1]["jobs"][0]["active"])

    def test_api_repairs_old_numeric_title_without_changing_job(self):
        job_id = self.db.add("123", "123", "공지", "2035-01-01T10:00",
                             "daily")
        status, result = self.fetch("/api/jobs")
        self.assertEqual(status, 200)
        job = next(item for item in result["jobs"] if item["id"] == job_id)
        self.assertEqual(job["room_title"], "업무방")
        self.assertEqual(job["message"], "공지")
        saved = self.db.list_jobs("123")[0]
        self.assertEqual(saved["room_title"], "업무방")
        self.assertEqual(saved["next_run"], job["next_run"])

    def test_web_registration_with_missing_name_is_repaired_later(self):
        self.client.rooms[0]["title"] = None
        status, result = self.fetch("/api/jobs", "POST",
                                    {"room_id": "123", "at": "2035-01-01T10:00",
                                     "repeat": "once", "message": "안내"})
        self.assertEqual(status, 201)
        initial = self.db.list_jobs("123")[0]
        self.assertEqual(initial["room_title"], "123")
        self.assertEqual(self.fetch("/api/jobs")[1]["jobs"][0]["room_title"], "123")
        self.client.rooms[0]["title"] = "업무방"
        row = self.fetch("/api/jobs")[1]["jobs"][0]
        self.assertEqual(row["room_title"], "업무방")
        self.assertEqual(self.db.list_jobs("123")[0]["room_title"], "업무방")
        self.assertEqual(row["id"], result["id"])
        self.assertEqual(row["message"], "안내")

    def test_history_name_repaired_without_changing_delivery(self):
        self.client.rooms[0]["title"] = "123"
        self.db.add("123", "123", "공지", "2035-01-01T10:00", "once")
        dispatch_once(self.db, lambda *args: {"submitted": True},
                      now=datetime(2035, 1, 1, 10, 0))
        before = self.db.history()[0]
        self.assertEqual(before["room_title"], "123")
        self.client.rooms[0]["title"] = "업무방"
        result = self.fetch("/api/history")[1]["history"][0]
        self.assertEqual(result["room_title"], "업무방")
        self.assertEqual(result["room_id"], "123")
        self.assertEqual(result["id"], before["id"])
        self.assertEqual(result["status"], before["status"])

    def test_unknown_room_rejected(self):
        with self.assertRaises(HTTPError) as ctx:
            self.fetch("/api/jobs", "POST",
                       {"room_id": "888", "at": "2035-01-01T10:00",
                        "repeat": "once", "message": "메시지"})
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()

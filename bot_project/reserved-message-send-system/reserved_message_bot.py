"""카카오톡 예약 발송과 로컬 관리 화면을 함께 실행한다."""
import argparse
import calendar
import json
import logging
import os
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from nichirin_client import NichirinClient, NichirinError

LOG = logging.getLogger("reservation-bot")
KST = timezone(timedelta(hours=9))
KINDS = ("once", "daily", "weekly", "monthly")
REPEATS = {"1회": "once", "한번": "once", "매일": "daily",
           "매주": "weekly", "매월": "monthly"}
LABELS = {"once": "1회", "daily": "매일", "weekly": "매주", "monthly": "매월"}
VIEW_MORE = "\u200b" * 500

HELP_TEXT = (
    "📅 예약 발송 봇 도움말\n" + VIEW_MORE + "\n"
    "━━━━━━━━━━━━\n\n"
    "📝 예약 추가\n"
    "  !예약 추가 YYYY-MM-DD HH:MM 1회 메시지\n"
    "  예: !예약 추가 2027-01-15 18:30 1회 회의 시작!\n\n"
    "🔁 반복 설정\n"
    "  1회 / 매일 / 매주 / 매월\n"
    "  예: !예약 추가 2027-01-15 09:00 매일 출석 확인\n\n"
    "📋 내 예약 확인: !예약 목록\n"
    "🗑️ 내 예약 삭제: !예약 삭제 1\n"
    "❓ 도움말: !예약도움말 / !예약\n\n"
    "🔒 내 예약만 조회·삭제되며, 완료·삭제 후 번호는 당겨집니다.\n"
    "🕒 모든 시간은 한국시간(KST)입니다."
)


def now_kst():
    return datetime.now(KST).replace(tzinfo=None)


def parse_time(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d", value):
        raise ValueError("날짜는 YYYY-MM-DDTHH:MM 형식으로 입력하세요.")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ValueError("유효하지 않은 날짜/시간입니다.") from exc


def next_time(current, repeat, anchor_day, after):
    if repeat == "once":
        return None
    candidate = current
    for _ in range(50000):
        if repeat == "daily":
            candidate += timedelta(days=1)
        elif repeat == "weekly":
            candidate += timedelta(weeks=1)
        elif repeat == "monthly":
            year = candidate.year + (candidate.month == 12)
            month = candidate.month % 12 + 1
            day = min(anchor_day, calendar.monthrange(year, month)[1])
            candidate = candidate.replace(year=year, month=month, day=day)
        else:
            raise ValueError("잘못된 반복 주기")
        if candidate > after:
            return candidate
    raise ValueError("다음 예약 시각 계산 범위 초과")


class SchedulerDB:
    def __init__(self, path):
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL, room_title TEXT NOT NULL,
                message TEXT NOT NULL, next_run TEXT,
                repeat TEXT NOT NULL, anchor_day INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_jobs_next ON jobs(active, next_run);
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL,
                due_at TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
                UNIQUE(job_id, due_at));
            CREATE INDEX IF NOT EXISTS idx_deliveries_job ON deliveries(job_id, id);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "owner_sender_id" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN owner_sender_id TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS idx_jobs_room_owner ON jobs(room_id, owner_sender_id)")
            # 발송 도중 중단된 건은 실제 전송 여부를 알 수 없어 재시도하지 않는다.
            db.execute("UPDATE deliveries SET status='uncertain', "
                       "detail='중단 중 전송 여부 불명 (자동 재시도 금지)' WHERE status='sending'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=15000")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def add(self, room_id, room_title, message, at, repeat, now=None, owner_sender_id=""):
        moment = parse_time(at)
        if moment <= (now or now_kst()):
            raise ValueError("예약 시간은 현재 시각 이후여야 합니다.")
        if repeat not in KINDS:
            raise ValueError("반복 주기는 once, daily, weekly, monthly 중 하나여야 합니다.")
        if not re.fullmatch(r"\d+", str(room_id)):
            raise ValueError("유효하지 않은 채팅방 ID입니다.")
        if not isinstance(message, str) or not message.strip() or len(message) > 3000:
            raise ValueError("메시지는 1~3000자로 입력하세요.")
        if not isinstance(owner_sender_id, str) or len(owner_sender_id) > 80:
            raise ValueError("작성자 ID가 올바르지 않습니다.")
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO jobs(room_id, room_title, message, next_run, repeat, "
                "anchor_day, created_at, owner_sender_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(room_id),
                    str(room_title)[:200],
                    message,
                    moment.isoformat(timespec="seconds"),
                    repeat,
                    moment.day,
                    (now or now_kst()).isoformat(timespec="seconds"),
                    owner_sender_id,
                ),
            )
            return cursor.lastrowid

    def has_pending_room_titles(self):
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM jobs WHERE room_title=room_id "
                "OR TRIM(room_title)='' LIMIT 1"
            ).fetchone() is not None

    def repair_room_titles(self, room_titles):
        """DB에 임시 ID로 기록된 예약만 확인된 채팅방 이름으로 바꾼다."""
        fixed = 0
        with self.connect() as db:
            pending = db.execute(
                "SELECT DISTINCT room_id FROM jobs "
                "WHERE room_title=room_id OR TRIM(room_title)=''"
            ).fetchall()
            for row in pending:
                room_id = row["room_id"]
                title = room_titles.get(room_id)
                if not isinstance(title, str) or not title.strip() or title.strip() == room_id:
                    continue
                result = db.execute(
                    "UPDATE jobs SET room_title=? WHERE room_id=? "
                    "AND (room_title=room_id OR TRIM(room_title)='')",
                    (title.strip()[:200], room_id),
                )
                fixed += result.rowcount
        return fixed

    def list_jobs(self, room_id=None, owner_sender_id=None):
        query = "SELECT * FROM jobs"
        filters, params = [], []
        if room_id is not None:
            filters.append("room_id=?")
            params.append(str(room_id))
        if owner_sender_id is not None:
            filters.append("owner_sender_id=?")
            params.append(str(owner_sender_id))
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY active DESC, next_run ASC, id DESC"
        with self.connect() as db:
            return [dict(row) for row in db.execute(query, params)]

    def list_active_owned(self, room_id, owner_sender_id):
        """목록 번호와 삭제 번호 모두 등록순으로 계산한다."""
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM jobs WHERE room_id=? AND owner_sender_id=? "
                "AND active=1 AND next_run IS NOT NULL "
                "ORDER BY id ASC",
                (str(room_id), str(owner_sender_id)),
            )]

    def cancel_owned_position(self, position, room_id, owner_sender_id):
        """내부 ID와 발송 이력은 그대로 두고 화면 순번에 해당하는 예약만 취소한다."""
        if (not isinstance(position, int) or position < 1
                or not str(room_id).isdigit() or not str(owner_sender_id).isdigit()):
            return False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM jobs WHERE room_id=? AND owner_sender_id=? "
                "AND active=1 AND next_run IS NOT NULL "
                "ORDER BY id ASC LIMIT 1 OFFSET ?",
                (str(room_id), str(owner_sender_id), position - 1),
            ).fetchone()
            if row is None:
                return False
            result = db.execute(
                "UPDATE jobs SET active=0, next_run=NULL "
                "WHERE id=? AND room_id=? AND owner_sender_id=? "
                "AND active=1 AND next_run IS NOT NULL",
                (row["id"], str(room_id), str(owner_sender_id)),
            )
            return result.rowcount == 1

    def history(self, limit=100):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT d.*, j.room_id, j.room_title, j.message FROM deliveries d "
                "JOIN jobs j ON j.id=d.job_id ORDER BY d.id DESC LIMIT ?", (min(limit, 300),))]

    def set_active(self, job_id, active):
        with self.connect() as db:
            result = db.execute(
                "UPDATE jobs SET active=? WHERE id=? AND next_run IS NOT NULL",
                (int(active), job_id),
            )
            return result.rowcount == 1

    def claim_due(self, now=None, max_lateness_seconds=300):
        current = now or now_kst()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE active=1 AND next_run <= ? "
                             "ORDER BY next_run, id LIMIT 1",
                             (current.isoformat(timespec="seconds"),)).fetchone()
            if row is None:
                return None
            job = dict(row)
            due = datetime.fromisoformat(job["next_run"])
            upcoming = next_time(due, job["repeat"], job["anchor_day"], current)
            db.execute("UPDATE jobs SET next_run=?, active=? WHERE id=?",
                       (upcoming.isoformat(timespec="seconds") if upcoming else None,
                        int(upcoming is not None), job["id"]))
            missed = (current - due).total_seconds() > max_lateness_seconds
            status = "missed" if missed else "sending"
            note = "발송 허용 지연(5분) 초과로 건너뜀" if missed else ""
            cursor = db.execute("INSERT INTO deliveries(job_id,due_at,started_at,finished_at,status,detail) "
                       "VALUES(?,?,?,?,?,?)",
                       (job["id"], job["next_run"], current.isoformat(timespec="seconds"),
                        current.isoformat(timespec="seconds") if missed else None, status, note))
            return {"delivery_id": cursor.lastrowid, "job": job, "missed": missed}

    def finish(self, delivery_id, status, detail):
        if status not in ("submitted", "uncertain"):
            raise ValueError("알 수 없는 전송 상태")
        with self.connect() as db:
            db.execute("UPDATE deliveries SET status=?, detail=?, finished_at=? "
                       "WHERE id=? AND status='sending'",
                       (status, str(detail)[:1000],
                        now_kst().isoformat(timespec="seconds"), delivery_id))


def dispatch_once(db, sender, now=None):
    claimed = db.claim_due(now=now)
    if not claimed:
        return False
    if claimed["missed"]:
        return True
    job = claimed["job"]
    message = f"[📅 예약 메시지]\n{job['message']}"
    try:
        result = sender(job["room_id"], message)
        db.finish(claimed["delivery_id"], "submitted", str(result))
    except Exception as exc:
        db.finish(claimed["delivery_id"], "uncertain", f"{type(exc).__name__}: {exc}")
    return True


def format_reservation_list(db, room_id, sender_id, room_name):
    jobs = db.list_active_owned(str(room_id), str(sender_id))
    if not jobs:
        return (f"📭 {room_name} 예약 목록\n━━━━━━━━━━━━\n\n"
                "현재 이 채팅방에 등록한 활성 예약이 없습니다.\n"
                "📝 등록 방법: !예약도움말")
    header = f"📋 {room_name} 예약 목록\n📌 [{room_name}] 채팅방 · 총 {len(jobs)}건"
    entries = []
    for display_number, job in enumerate(jobs[:15], 1):
        preview = job["message"].replace("\r", " ").replace("\n", " ").strip()
        if len(preview) > 65:
            preview = preview[:65] + "…"
        entry = (f"📨 #{display_number}  {LABELS.get(job['repeat'], job['repeat'])}\n"
                 f"🕒 {job['next_run'][:16].replace('T', ' ')} KST\n"
                 f"💬 {preview}")
        if len("\n\n".join([header] + entries + [entry])) > 1400:
            break
        entries.append(entry)
    result = "\n\n".join([header] + entries)
    if len(jobs) > len(entries):
        result += f"\n\n📎 나머지 {len(jobs) - len(entries)}건은 목록에서 생략했습니다."
    return result


def add_reservation_from_command(db, command, room_id, sender_id, room_name, now):
    match = re.fullmatch(
        r"!예약\s+추가\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s+(\S+)\s+(.+)",
        command, flags=re.DOTALL)
    if match is None:
        return ("⚠️ 예약 형식이 올바르지 않습니다.\n"
                "예: !예약 추가 2027-01-15 18:30 1회 회의 시작!\n"
                "❓ !예약도움말")
    day, clock, repeat_text, message = match.groups()
    repeat = REPEATS.get(repeat_text)
    if repeat not in KINDS:
        return "⚠️ 반복 주기는 1회, 매일, 매주, 매월 중 선택하세요."
    pending_title = room_name == str(room_id)
    try:
        job_id = db.add(str(room_id), room_name, message,
                        f"{day}T{clock}", repeat, now=now,
                        owner_sender_id=str(sender_id))
    except ValueError as exc:
        return f"⚠️ 예약 등록 실패\n{exc}\n❓ !예약도움말"
    owned = db.list_active_owned(str(room_id), str(sender_id))
    display_number = next((i for i, job in enumerate(owned, 1)
                           if job["id"] == job_id), None)
    number_text = f" · #{display_number}" if display_number is not None else ""
    name_note = "\nℹ️ 채팅방 이름 확인 후 자동으로 갱신됩니다." if pending_title else ""
    return (f"✅ 예약 등록 완료{number_text}\n"
            f"📌 [{room_name}] 채팅방에 전송{name_note}\n"
            f"🕒 {day} {clock} KST\n"
            f"🔁 {LABELS[repeat]}\n"
            f"💬 {message[:180]}{'…' if len(message) > 180 else ''}\n\n"
            "📋 확인: !예약 목록")


def answer(db, text, room_id, sender_id, room_title=None, now=None):
    """현재 채팅방과 작성자가 가진 예약만 조회·수정한다."""
    if not (str(room_id).isdigit() and str(sender_id).isdigit()):
        return None
    command = text.strip() if isinstance(text, str) else ""
    room_name = str(room_title or room_id)
    if command in ("!예약", "!예약도움말", "!예약 도움말", "!예약발송도움말"):
        return HELP_TEXT

    if command == "!예약 목록":
        return format_reservation_list(db, room_id, sender_id, room_name)

    if command.startswith("!예약 삭제"):
        match = re.fullmatch(r"!예약\s+삭제\s+([1-9][0-9]*)", command)
        if match is None:
            return "⚠️ 삭제할 예약 번호를 입력하세요.\n예: !예약 삭제 1\n📋 !예약 목록"
        display_number = int(match.group(1))
        if not db.cancel_owned_position(display_number, str(room_id), str(sender_id)):
            return ("⚠️ 삭제할 수 없는 예약입니다.\n"
                    "번호가 변경되었을 수 있으니 최신 내 예약 목록을 확인하세요.\n"
                    "📋 !예약 목록")
        return (f"🗑️ 예약 #{display_number} 삭제 완료\n"
                "이 예약의 향후 발송을 취소했습니다.\n"
                "남은 예약 번호는 자동으로 당겨집니다.\n📋 !예약 목록")

    if command.startswith("!예약 추가"):
        return add_reservation_from_command(db, command, room_id, sender_id, room_name, now)

    if command.startswith("!예약"):
        return "❓ 사용할 수 없는 예약 명령입니다.\n명령어 확인: !예약도움말"
    return None


def handle_message(db, message, room_titles, cursor, sender=None, now=None):
    """이미 처리했거나 봇 자신이 보낸 메시지는 건너뛴다."""
    if not isinstance(message, dict):
        return None
    room_id, log_id = str(message.get("roomId", "")), str(message.get("id", ""))
    if not (room_id.isdigit() and log_id.isdigit()):
        return None
    if int(log_id) <= int(cursor.get(room_id, "0")):
        return None
    cursor[room_id] = log_id
    if message.get("fromMe") is not False or message.get("kind") not in ("text", "reply"):
        return None
    sender_id = str(message.get("senderId", ""))
    if not sender_id.isdigit():
        LOG.warning("Sender ID unavailable in room %s; command ignored", room_id)
        return None
    if isinstance(message.get("text"), str) and message["text"].strip().startswith("!예약"):
        LOG.info("Reservation command received: roomId=%s logId=%s", room_id, log_id)
    reply = answer(db, message.get("text"), room_id, sender_id,
                   room_title=room_titles.get(room_id), now=now)
    if reply and sender is not None:
        sender(room_id, reply)
    return reply


def verified_room_titles(rooms):
    """Nichirin이 실제 이름을 반환한 방만 이름 매핑에 포함한다."""
    titles = {}
    for room in rooms:
        if not isinstance(room, dict):
            continue
        room_id = str(room.get("id", ""))
        title = room.get("title")
        if (room_id.isdigit() and isinstance(title, str)
                and title.strip() and title.strip() != room_id):
            titles[room_id] = title.strip()
    return titles


def update_pending_titles(client, db, stop):
    """SSE가 대기 중이어도 임시로 저장된 이름을 정기적으로 복구한다."""
    while not stop.wait(30):
        try:
            if not db.has_pending_room_titles():
                continue
            names = verified_room_titles(client._request("/rooms").get("rooms", []))
            repaired = db.repair_room_titles(names)
            if repaired:
                LOG.info("Updated %s pending reservation room names", repaired)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            LOG.warning("Room title refresh failed: %s", exc)


def run_chat_bot(client, db, stop):
    """읽을 수 있는 채팅방의 예약 명령을 감시한다."""
    cursor = {}
    room_titles = {}
    delay = 1
    initialized = False
    while not stop.is_set():
        try:
            rooms = client._request("/rooms").get("rooms", [])
            room_titles.update(verified_room_titles(rooms))
            repaired = db.repair_room_titles(room_titles)
            if repaired:
                LOG.info("Corrected %s saved reservation room titles", repaired)
            if not initialized:
                # 아직 읽을 수 없는 방도 기준 메시지 ID를 보존한다.
                # 나중에 복호화 키가 생길 때 첫 명령을 놓치지 않도록 하기 위해서다.
                for room in rooms:
                    rid = str(room.get("id", ""))
                    latest = str(room.get("lastMessageId") or "0")
                    if rid.isdigit() and latest.isdigit():
                        cursor[rid] = latest
                initialized = True
                LOG.info("Reservation stream initialized with %s room cursors", len(cursor))
            for event, payload in client.stream(cursor):
                if stop.is_set():
                    return
                if event == "error":
                    raise RuntimeError(str(payload.get("error", payload)))
                if event != "message" or not payload.get("ok"):
                    continue
                try:
                    message = payload.get("message")
                    if isinstance(message, dict):
                        room_id = str(message.get("roomId", ""))
                        command = message.get("text", "")
                        if (room_id not in room_titles and isinstance(command, str)
                                and command.strip().startswith("!예약")):
                            latest = client._request("/rooms").get("rooms", [])
                            room_titles.update(verified_room_titles(latest))
                            db.repair_room_titles(room_titles)
                    handle_message(db, message, room_titles, cursor,
                                   sender=client.send)
                except (ValueError, sqlite3.Error) as exc:
                    LOG.warning("Chat command failed: %s", exc)
                delay = 1
        except (OSError, ValueError, RuntimeError) as exc:
            LOG.warning("SSE connection interrupted: %s", exc)
        except Exception:
            LOG.exception("Unexpected reservation bot error")
        if stop.wait(delay):
            break
        delay = min(delay * 2, 30)


def make_handler(db, client, live):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            LOG.info("%s %s", self.address_string(), fmt % args)

        def respond(self, code, data):
            blob = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(blob)

        def local_request(self, api=False):
            # Loopback 바인딩에 더해 DNS 리바인딩과 다른 사이트의 API 호출을 차단한다.
            port = self.server.server_port
            host = self.headers.get("Host", "").lower()
            if (self.client_address[0] != "127.0.0.1"
                    or host not in (f"127.0.0.1:{port}", f"localhost:{port}")):
                self.respond(403, {"error": "로컬 PC에서만 접속할 수 있습니다."})
                return False
            if api:
                origin = self.headers.get("Origin")
                if origin and origin.lower() != f"http://{host}":
                    self.respond(403, {"error": "외부 사이트의 요청은 허용하지 않습니다."})
                    return False
                site = self.headers.get("Sec-Fetch-Site", "")
                if site and site not in ("same-origin", "none"):
                    self.respond(403, {"error": "외부 사이트의 요청은 허용하지 않습니다."})
                    return False
            return True

        def named_rows(self, rows):
            if client is None:
                return rows
            try:
                names = verified_room_titles(client._request("/rooms").get("rooms", []))
            except (OSError, NichirinError, ValueError):
                return rows
            db.repair_room_titles(names)
            for row in rows:
                row["room_title"] = names.get(str(row.get("room_id")),
                                              row["room_title"])
            return rows

        def do_GET(self):
            target = urlparse(self.path)
            if not self.local_request(api=target.path.startswith("/api/")):
                return
            static_files = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
                "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
            }
            if target.path in static_files:
                filename, mime_type = static_files[target.path]
                content = (ROOT / "admin_web" / filename).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(content)
                return
            try:
                if target.path == "/api/status":
                    self.respond(200, {"live": live, "now": now_kst().isoformat(timespec="seconds"),
                                       "timezone": "Asia/Seoul"})
                elif target.path == "/api/rooms":
                    if client is None:
                        self.respond(503, {"error": "KANAO_TOKEN을 설정하여 Nichirin에 연결하세요."})
                    else:
                        rooms = client._request("/rooms").get("rooms", [])
                        db.repair_room_titles(verified_room_titles(rooms))
                        self.respond(200, {"rooms": rooms})
                elif target.path == "/api/jobs":
                    room = parse_qs(target.query).get("room", [None])[0]
                    self.respond(200, {"jobs": self.named_rows(db.list_jobs(room))})
                elif target.path == "/api/history":
                    self.respond(200, {"history": self.named_rows(db.history())})
                else:
                    self.respond(404, {"error": "경로 없음"})
            except (OSError, NichirinError, ValueError) as exc:
                self.respond(503, {"error": str(exc)})

        def do_POST(self):
            if not self.local_request(api=True):
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self.respond(415, {"error": "JSON 요청만 허용됩니다."})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 2 or size > 10000:
                    raise ValueError("요청 크기가 올바르지 않습니다.")
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError("JSON 객체가 필요합니다.")
                target = urlparse(self.path).path
                if target == "/api/jobs":
                    if client is None:
                        self.respond(503, {"error": "KANAO_TOKEN이 필요합니다."})
                        return
                    room_id = str(payload.get("room_id", ""))
                    rooms = client._request("/rooms").get("rooms", [])
                    selected = [r for r in rooms if str(r.get("id")) == room_id]
                    if len(selected) != 1:
                        raise ValueError("현재 조회 가능한 채팅방 ID를 선택하세요.")
                    title = verified_room_titles(selected).get(room_id, room_id)
                    new_id = db.add(room_id, title,
                        payload.get("message"), payload.get("at"), payload.get("repeat"))
                    self.respond(201, {"id": new_id})
                    return
                parts = target.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] in ("enable", "disable"):
                    job_id = int(parts[2])
                    ok = db.set_active(job_id, parts[3] == "enable")
                    self.respond(200 if ok else 404, {"ok": ok})
                    return
                self.respond(404, {"error": "경로 없음"})
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                self.respond(400, {"error": str(exc)})
            except (OSError, NichirinError) as exc:
                self.respond(503, {"error": str(exc)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Nichirin 카카오톡 예약 발송 관리자")
    parser.add_argument("--live", action="store_true", help="실제 발송 허용 (기본: 조회·등록만)")
    parser.add_argument("--check", action="store_true", help="Nichirin 연결 확인 (전송 없음)")
    parser.add_argument("--list", action="store_true", help="예약 목록 CLI 출력")
    parser.add_argument("--room", help="--list와 함께 사용할 채팅방 ID")
    parser.add_argument("--db", default=str(ROOT / "data" / "reservations.sqlite3"))
    parser.add_argument("--port", type=int, default=8848)
    args = parser.parse_args()
    db = SchedulerDB(args.db)
    token = os.environ.get("KANAO_TOKEN", "")
    client = NichirinClient(os.environ.get("KANAO_URL", "http://127.0.0.1:8765"), token) if token else None
    if args.list:
        print(json.dumps(db.list_jobs(args.room), ensure_ascii=False, indent=2))
        return
    if args.check:
        if client is None:
            parser.error("KANAO_TOKEN을 설정하세요")
        result = client._request("/rooms")
        print(f"Nichirin 연결됨: 채팅방 {len(result.get('rooms', []))}개 (발송 없음)")
        return
    if args.live and client is None:
        parser.error("--live 실행에는 KANAO_TOKEN이 필요합니다.")
    stop = threading.Event()
    if args.live:
        def dispatch_loop():
            while not stop.is_set():
                try:
                    if dispatch_once(db, client.send):
                        continue
                except Exception:
                    LOG.exception("예약 처리 오류")
                stop.wait(1)
        threading.Thread(target=dispatch_loop, name="reserved-dispatch", daemon=True).start()
        threading.Thread(target=run_chat_bot, args=(client, db, stop),
                         name="reserved-room-chat", daemon=True).start()
        threading.Thread(target=update_pending_titles, args=(client, db, stop),
                         name="reserved-room-titles", daemon=True).start()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(db, client, args.live))
    print(f"관리자 페이지: http://127.0.0.1:{args.port}/")
    print("실제 전송: " + ("활성화" if args.live else "비활성화 (미리보기 모드)"))
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("--live")
    if "--live" in sys.argv[1:]:
        print("[주의] 예약된 카카오톡 메시지 실제 발송 모드입니다.")
    main()

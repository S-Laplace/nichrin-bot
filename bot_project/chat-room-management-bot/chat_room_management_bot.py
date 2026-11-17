"""Nichirin 채팅방 관리 봇: 관리자 명령 처리 및 SSE 감시."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class NichirinError(RuntimeError):
    pass


def iter_sse(lines: Iterable[bytes]) -> Iterator[tuple[str, dict]]:
    """SSE의 event/data 필드를 파싱해 (이벤트, JSON) 쌍을 반환."""
    event = "message"
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                try:
                    payload = json.loads("\n".join(data))
                except json.JSONDecodeError as exc:
                    raise NichirinError("SSE JSON 파싱 실패") from exc
                if not isinstance(payload, dict):
                    raise NichirinError("SSE 데이터가 객체가 아닙니다")
                yield event, payload
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        payload = json.loads("\n".join(data))
        if isinstance(payload, dict):
            yield event, payload


class NichirinClient:
    def __init__(self, base_url: str, token: str):
        if not token:
            raise ValueError("Nichirin Bearer 토큰이 필요합니다")
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def _request(self, path: str, body: dict | None = None) -> dict:
        headers = dict(self.headers)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(self.base_url + path, data=data, headers=headers)
        try:
            with urlopen(request, timeout=20) as response:
                result = json.load(response)
        except HTTPError as exc:
            # Nichirin은 {"error": {"code": ..., "message": ...}}를 반환한다.
            # HTTP 상태만 기록하면 WINDOW_NOT_FOUND 같은 원인을 놓친다.
            detail = ""
            try:
                payload = json.load(exc)
                error = payload.get("error", {}) if isinstance(payload, dict) else {}
                if isinstance(error, dict):
                    code = error.get("code")
                    message = error.get("message")
                    if code:
                        detail += f" [{code}]"
                    if message:
                        detail += f" {message}"
            except (ValueError, UnicodeDecodeError, OSError):
                pass
            raise NichirinError(f"HTTP {exc.code}: {exc.reason}{detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise NichirinError(f"Nichirin 연결 실패: {exc}") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise NichirinError(f"Nichirin 오류: {result!r}")
        return result

    def send(self, room_id: str, text: str) -> dict:
        result = self._request("/send", {
            "room": str(room_id),
            "text": text,
            "openRoom": True,
        })
        if not result.get("submitted"):
            raise NichirinError("메시지 입력 성공 여부를 확인할 수 없습니다")
        return result

    def stream(self, cursor: dict[str, str] | None = None) -> Iterator[tuple[str, dict]]:
        query = ""
        if cursor:
            query = "?" + urlencode([("since", f"{room}:{log_id}")
                                      for room, log_id in cursor.items()])
        req = Request(
            self.base_url + "/stream" + query,
            headers={**self.headers, "Accept": "text/event-stream"},
        )
        try:
            with urlopen(req, timeout=90) as response:
                yield from iter_sse(response)
        except HTTPError as exc:
            raise NichirinError(f"SSE HTTP {exc.code}: {exc.reason}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise NichirinError(f"SSE 연결 실패: {exc}") from exc


ADMIN_ROOM_TITLE = "ADMINROOM"
MAX_ROOMS = 40
VIEW_MORE = "\u200b" * 500
HELP = (
    "📖 채팅방 관리봇 도움말\n" + VIEW_MORE + "\n\n"
    "📌 명령어\n"
    "🔐 !방활성화 채팅방이름 — 복호화 키 확보 및 전송 준비 확인\n"
    "➕ !방등록 채팅방이름 — 관리 목록 등록 및 활성화\n"
    "🗑️ !방삭제 채팅방이름 — 관리 목록에서 삭제\n"
    "📋 !방목록 — 등록된 채팅방 상태 조회\n"
    "🔄 !방전체활성화 — 비활성 등록 채팅방 활성화\n"
    "🔎 !방상태 채팅방이름 — 채팅방 상태 확인\n"
    "❓ !채팅방관리도움말 — 도움말 보기"
)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _number(value: object) -> bool:
    return str(value).isdigit()


class Registry:
    """방 이름 대신 변하지 않는 방 ID를 저장한다."""
    def __init__(self, path: Path):
        self.path = path
        self.rooms: dict[str, str] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("rooms"), dict):
                raise ValueError("rooms.json 형식 오류: 기존 파일을 덮어쓰지 않습니다")
            for rid, name in data["rooms"].items():
                if not _number(rid) or not isinstance(name, str):
                    raise ValueError("rooms.json에 잘못된 방 정보가 있습니다")
                self.rooms[str(rid)] = name

    def register(self, rid: str, title: str) -> bool:
        if rid in self.rooms:
            return False
        if len(self.rooms) >= MAX_ROOMS:
            raise ValueError(f"관리할 수 있는 채팅방은 최대 {MAX_ROOMS}개입니다")
        self.rooms[rid] = title
        _write_json(self.path, {"rooms": self.rooms})
        return True

    def delete(self, rid: str) -> bool:
        if rid not in self.rooms:
            return False
        del self.rooms[rid]
        _write_json(self.path, {"rooms": self.rooms})
        return True


class Cursor:
    """중복 명령·재접속 중복 발송 방지. 처리 직전에 기록한다."""
    def __init__(self, path: Path):
        self.path = path
        self.last: dict[str, str] = {}
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("커서 파일 형식 오류")
            for rid, mid in data.items():
                if _number(rid) and _number(mid):
                    self.last[str(rid)] = str(mid)

    def advance(self, rid: str, mid: str) -> bool:
        if not _number(rid) or not _number(mid):
            return False
        if int(mid) <= int(self.last.get(rid, "0")):
            return False
        self.last[rid] = mid
        _write_json(self.path, self.last)
        return True



def parse_command(text: object) -> tuple[str, str] | None:
    if not isinstance(text, str):
        return None
    value = text.strip()
    if value == "!채팅방관리도움말":
        return ("도움말", "")
    match = re.fullmatch(
        r"!(?:방|채팅방)\s*(전체활성화|일괄활성화|활성화|등록|삭제|목록|상태|점검)(?:\s+(.+))?",
        value, flags=re.DOTALL,
    )
    if match is None:
        return None
    action, argument = match.groups()
    return ({"일괄활성화": "전체활성화", "점검": "목록"}.get(action, action),
            (argument or "").strip())


@dataclass(frozen=True)
class RoomStatus:
    readable: bool | None
    input_ready: bool | None
    input_note: str = ""

    @property
    def ready(self) -> bool:
        return self.readable is True and self.input_ready is True

    def lines(self) -> str:
        if self.readable is True:
            read = "🔑 읽기: ✅ 가능 (복호화 키 확인)"
        elif self.readable is False:
            read = "🔑 읽기: ❌ 불가 (복호화 키 미확보)"
        else:
            read = "🔑 읽기: ⚪ 확인 불가 (채팅방 상태 없음)"
        if self.input_ready is True:
            sender = "⌨️ 전송 입력창: ✅ 확인됨"
        else:
            sender = f"⌨️ 전송 입력창: ⚠️ 미확인 ({self.input_note or '점검 불가'})"
        return read + "\n" + sender


class Manager:
    def __init__(self, client, registry: Registry):
        self.client = client
        self.registry = registry
        self.admin_id: str | None = None

    def rooms(self) -> list[dict]:
        rows = self.client._request("/rooms").get("rooms", [])
        if not isinstance(rows, list):
            raise NichirinError("서버 채팅방 목록의 형식이 잘못되었습니다")
        return [r for r in rows if isinstance(r, dict) and _number(r.get("id"))]

    def exact_room(self, title: str, rows: list[dict] | None = None) -> dict:
        found = [r for r in (self.rooms() if rows is None else rows)
                 if r.get("title") == title]
        if not found:
            raise ValueError(f"'{title}' 채팅방을 찾지 못했습니다")
        if len(found) != 1:
            raise ValueError(f"'{title}' 이름의 방이 여러 개입니다. 고유한 채팅방 이름으로 바꿔주세요")
        return found[0]

    def admin_room(self) -> dict:
        room = self.exact_room(ADMIN_ROOM_TITLE)
        self.admin_id = str(room["id"])
        return room

    def _dry_run(self, rid: str, *, allow_open: bool) -> None:
        result = self.client._request("/send", {
            "room": rid, "text": "[채팅방관리봇 전송 점검 - 발송하지 않음]",
            "dryRun": True, "openRoom": allow_open,
        })
        if result.get("dryRun") is not True or result.get("submitted") is True:
            raise NichirinError("Nichirin 시험 발송 응답을 확인할 수 없습니다")

    def ensure_ready(self, room: dict) -> None:
        """키를 확보하고 실제 수신/전송 경로를 점검. 대상 방에는 메시지 발송 안 함."""
        rid = str(room["id"])
        request = "/context?" + urlencode({"roomId": rid, "limit": "1", "open": "true"})
        try:
            self.client._request(request)
        except NichirinError as exc:
            if "[KEY_UNAVAILABLE]" not in str(exc):
                raise
            # 채팅방을 열어 둔 상태라면 키 검색이 끝나기 전에도 다시 열지 않는다.
            retry = "/context?" + urlencode({"roomId": rid, "limit": "1", "open": "false"})
            for seconds in (0.5, 1.0, 2.0):
                time.sleep(seconds)
                try:
                    self.client._request(retry)
                    break
                except NichirinError as retry_error:
                    if "[KEY_UNAVAILABLE]" not in str(retry_error):
                        raise
            else:
                raise NichirinError("채팅방을 열었지만 복호화 키를 확보하지 못했습니다. 다시 시도해 주세요.") from exc
        current = next((r for r in self.rooms() if str(r["id"]) == rid), None)
        if current is None or current.get("readable") is not True:
            raise NichirinError("복호화 키 확보 후에도 채팅방이 읽기 가능으로 표시되지 않습니다")
        self._dry_run(rid, allow_open=True)

    def check_status(self, room: dict | None) -> RoomStatus:
        """카카오톡 방을 열지 않고 읽기/전송 입력창 상태를 별도로 확인한다."""
        if room is None:
            return RoomStatus(None, None, "채팅방 목록에 없음")

        flag = room.get("readable")
        readable = flag if isinstance(flag, bool) else None
        try:
            self._dry_run(str(room["id"]), allow_open=False)
        except (NichirinError, ValueError) as exc:
            detail = str(exc)
            if "WINDOW_NOT_FOUND" in detail:
                reason = "채팅방 창 없음"
            elif "INPUT_NOT_FOUND" in detail:
                reason = "입력창을 찾지 못함"
            else:
                reason = "시험 점검 실패"
            return RoomStatus(readable, False, reason)
        return RoomStatus(readable, True)

    def send_admin(self, content: str) -> None:
        if self.admin_id is None:
            raise RuntimeError("관리자 방 ID가 설정되지 않았습니다")
        text = content if len(content) <= 3000 else content[:2960] + "\n…(이하 생략)"
        self.client.send(self.admin_id, text)

    def activate_admin(self) -> None:
        room = self.admin_room()
        self.ensure_ready(room)
        # 등록 방 작업보다 반드시 먼저 관리자 방 활성화 알림을 보낸다.
        self.send_admin("nichirin bot이 활성화되었습니다.")

    @staticmethod
    def _problem_text(reason: str) -> str:
        if "KEY_UNAVAILABLE" in reason:
            return "복호화 키를 확보하지 못했습니다. 채팅방을 열고 다시 시도해 주세요."
        if "WINDOW_NOT_FOUND" in reason:
            return "현재 채팅방 창을 찾지 못했습니다."
        if "INPUT_NOT_FOUND" in reason:
            return "전송 입력창을 찾지 못했습니다."
        return reason

    def _activate_room(self, room: dict) -> tuple[RoomStatus, str | None]:
        problem = None
        try:
            self.ensure_ready(room)
        except (NichirinError, ValueError) as exc:
            problem = str(exc)
        current = next((r for r in self.rooms()
                        if str(r["id"]) == str(room["id"])), None)
        status = self.check_status(current)
        if problem is None and not status.ready:
            problem = "활성화 후 재확인에서 두 상태가 모두 확인되지 않았습니다"
        return status, problem

    @staticmethod
    def room_label(title: object, rid: str) -> str:
        name = str(title or "").strip()
        return name if name and name != rid else "이름 미확인"

    def _activation(self, title: str) -> str:
        room = self.exact_room(title)
        status, problem = self._activate_room(room)
        heading = "✅ 채팅방 활성화 확인" if problem is None else "⚠️ 채팅방 활성화 미완료"
        result = (f"{heading}\n\n"
                  f"📌 채팅방: {self.room_label(room.get('title'), str(room['id']))}\n"
                  f"{status.lines()}")
        if problem:
            result += f"\n\n🔎 사유: {self._problem_text(problem)[:180]}"
        else:
            result += "\n\nℹ️ 입력창 확인은 실제 메시지 전달을 보증하지 않습니다."
        return result

    def _register(self, title: str) -> str:
        room = self.exact_room(title)
        rid = str(room["id"])
        if rid == self.admin_id:
            return (f"ℹ️ 기본 관리자 채팅방\n\n"
                    f"📌 채팅방: {ADMIN_ROOM_TITLE}\n"
                    "🗂️ 관리 목록: 기본 관리자 (별도 등록 불필요)\n"
                    + self.check_status(room).lines())
        added = self.registry.register(rid, str(room["title"]))
        status, problem = self._activate_room(room)
        heading = "✅ 채팅방 등록 완료" if added else "ℹ️ 이미 등록된 채팅방"
        result = (f"{heading}\n\n"
                  f"📌 채팅방: {self.room_label(room.get('title'), rid)}\n"
                  "🗂️ 관리 목록: 등록됨\n"
                  f"🔄 활성화: {'✅ 확인' if problem is None else '⚠️ 미완료'}\n"
                  f"{status.lines()}")
        if problem:
            result += f"\n\n🔎 사유: {self._problem_text(problem)[:180]}\n📌 등록 정보는 유지됩니다."
        return result

    def _delete(self, title: str) -> str:
        matches = [(rid, name) for rid, name in self.registry.rooms.items() if name == title]
        matches += [(str(r["id"]), title) for r in self.rooms()
                    if r.get("title") == title and str(r["id"]) in self.registry.rooms
                    and str(r["id"]) not in {rid for rid, _ in matches}]
        if len(matches) != 1:
            return (f"⚠️ 채팅방 삭제 실패\n\n"
                    f"🔎 '{title}' 등록 채팅방을 하나로 특정할 수 없습니다.\n"
                    "🗂️ 관리 목록: 변경 없음\n"
                    "📋 목록 확인: !방목록")
        rid, name = matches[0]
        room = next((r for r in self.rooms() if str(r["id"]) == rid), None)
        self.registry.delete(rid)
        status = self.check_status(room)
        return (f"🗑️ 채팅방 등록 삭제 완료\n\n"
                f"📌 채팅방: {self.room_label(name, rid)}\n"
                f"🗂️ 관리 목록: 삭제됨\n"
                f"{status.lines()}\n\n"
                "ℹ️ 읽기·전송 상태와 다른 봇은 변경하지 않았습니다.")

    def _list(self) -> str:
        rows = self.rooms()
        by_id = {str(r["id"]): r for r in rows}
        lines = ["📋 관리 채팅방 목록",
                 f"📌 추가 등록: {len(self.registry.rooms)}개",
                 f"\n🔒 관리자: {ADMIN_ROOM_TITLE}",
                 self.check_status(by_id.get(self.admin_id)).lines()]
        if not self.registry.rooms:
            lines.extend(["\n📭 등록된 추가 채팅방이 없습니다.",
                          "➕ 등록: !방등록 채팅방이름"])
        else:
            for number, (rid, stored) in enumerate(self.registry.rooms.items(), 1):
                room = by_id.get(rid)
                current_title = room.get("title") if room else None
                title = self.room_label(current_title or stored, rid)
                if title == "이름 미확인":
                    title += f" #{number}"
                lines.extend([f"\n📍 {title}", self.check_status(room).lines()])
        lines.append("\nℹ️ 입력창 확인은 실제 메시지 전달을 보증하지 않습니다.")
        return "\n".join(lines)

    def _status(self, title: str) -> str:
        room = self.exact_room(title)
        rid = str(room["id"])
        registered = rid in self.registry.rooms or rid == self.admin_id
        return (f"🔎 채팅방 상태\n\n"
                f"📌 채팅방: {self.room_label(room.get('title'), rid)}\n"
                f"🗂️ 관리 등록: {'예' if registered else '아니요'}\n"
                f"{self.check_status(room).lines()}\n\n"
                "ℹ️ 입력창 확인은 실제 메시지 전달을 보증하지 않습니다.")

    def _bulk(self) -> str:
        rows = self.rooms()
        by_id = {str(r["id"]): r for r in rows}
        activated = skipped = failed = 0
        details = []
        for number, (rid, stored) in enumerate(self.registry.rooms.items(), 1):
            room = by_id.get(rid)
            current_title = room.get("title") if room else None
            title = self.room_label(current_title or stored, rid)
            if title == "이름 미확인":
                title += f" #{number}"
            if room is None:
                status, problem = self.check_status(None), "채팅방 목록에 없음"
                outcome = "⚠️ 채팅방 미확인"
                failed += 1
            else:
                initial = self.check_status(room)
                if initial.ready:
                    status, problem = initial, None
                    outcome = "ℹ️ 이미 준비됨"
                    skipped += 1
                else:
                    status, problem = self._activate_room(room)
                    if problem is None:
                        outcome = "✅ 활성화 확인"
                        activated += 1
                    else:
                        outcome = "⚠️ 활성화 미완료"
                        failed += 1
            lines = [f"📍 {title} — {outcome}", status.lines()]
            if problem:
                lines.append(f"🔎 사유: {self._problem_text(problem)[:130]}")
            details.append("\n".join(lines))

        heading = "🔄 채팅방 일괄 활성화 결과"
        totals = (f"✅ 새로 활성화: {activated}개\n"
                  f"ℹ️ 이미 준비됨: {skipped}개\n"
                  f"⚠️ 활성화 미완료: {failed}개")
        if not details:
            return heading + "\n\n📭 등록된 채팅방이 없습니다.\n➕ 등록: !방등록 채팅방이름"
        return (heading + "\n\n" + totals + "\n\n"
                + "\n\n".join(details)
                + "\n\nℹ️ 입력창 확인은 실제 메시지 전달을 보증하지 않습니다.")

    def answer(self, message: dict) -> str | None:
        """관리 방에서 온 다른 계정의 텍스트 명령만 처리한다."""
        if not isinstance(message, dict) or self.admin_id is None:
            return None
        if (str(message.get("roomId")) != self.admin_id
                or message.get("fromMe") is not False
                or message.get("kind") not in ("text", "reply")
                or not _number(message.get("senderId"))
                or not _number(message.get("id"))):
            return None
        parsed = parse_command(message.get("text"))
        if parsed is None:
            return None
        action, title = parsed
        if action in ("도움말", "목록", "전체활성화") and title:
            return "⚠️ 명령어 형식 오류\n\n🔎 불필요한 인자를 제거하세요.\n❓ !채팅방관리도움말"
        if action in ("등록", "삭제", "활성화", "상태") and not title:
            return (f"⚠️ 채팅방 이름이 필요합니다.\n\n"
                    f"✍️ 입력 예시: !방{action} 친목방\n"
                    "❓ !채팅방관리도움말")
        try:
            if action == "도움말":
                return HELP
            if action == "목록":
                return self._list()
            if action == "전체활성화":
                return self._bulk()
            if action == "상태":
                return self._status(title)
            if action == "등록":
                return self._register(title)
            if action == "삭제":
                return self._delete(title)
            if action == "활성화":
                return self._activation(title)
        except (ValueError, NichirinError) as exc:
            return (f"⚠️ 채팅방 {action} 실패\n\n"
                    f"🔎 사유: {str(exc)[:250]}\n"
                    "❓ 명령어 확인: !채팅방관리도움말")
        return None


LOG = logging.getLogger("chat_room_management_bot")
BOT_DIR = Path(__file__).resolve().parent
STATE_DIR = BOT_DIR / "data"


@contextmanager
def single_instance():
    """같은 봇을 실수로 두 번 실행해 관리자 방에 중복 응답하는 일 방지."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = (STATE_DIR / "running.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"1")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("채팅방관리봇이 이미 실행 중입니다") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def watch(client: NichirinClient, manager: Manager) -> None:
    assert manager.admin_id is not None
    admin_id = manager.admin_id
    cursor = Cursor(STATE_DIR / "cursor.json")
    room = next((r for r in manager.rooms() if str(r["id"]) == admin_id), None)
    if room is None:
        raise RuntimeError("관리자 방 정보를 읽을 수 없습니다")
    cursor.advance(admin_id, str(room.get("lastMessageId") or "0"))
    delay = 1
    LOG.info("채팅방관리봇 시작: 관리자 채팅방 '%s' (ID %s)", ADMIN_ROOM_TITLE, admin_id)
    while True:
        try:
            for event, payload in client.stream({admin_id: cursor.last.get(admin_id, "0")}):
                if event == "error":
                    raise NichirinError(f"SSE 이벤트 오류: {payload.get('error')}")
                if event != "message" or payload.get("ok") is not True:
                    continue
                message = payload.get("message")
                if not isinstance(message, dict):
                    continue
                if str(message.get("roomId")) != admin_id:
                    continue
                mid = str(message.get("id") or "")
                if not cursor.advance(admin_id, mid):
                    continue
                response = manager.answer(message)
                if response is None:
                    continue
                LOG.info("관리자 명령 처리: messageId=%s", mid)
                try:
                    manager.send_admin(response)
                except NichirinError as exc:
                    # 전송 완료 여부가 불분명해도 동일 명령을 자동으로 재전송하지 않는다.
                    LOG.error("명령 응답 전송 실패 (자동 재전송 안 함): %s", exc)
                delay = 1
            LOG.warning("SSE 스트림이 종료되어 재연결합니다")
        except (NichirinError, OSError, ValueError) as exc:
            LOG.warning("SSE 연결 문제: %s", exc)
        time.sleep(delay)
        delay = min(delay * 2, 30)


def main() -> int:
    parser = argparse.ArgumentParser(description="Nichirin 채팅방관리봇 (지정한 관리자 채팅방 전용)")
    parser.add_argument("--server", default=os.environ.get("KANAO_URL") or "http://127.0.0.1:8765",
                        help="Nichirin serve 주소")
    parser.add_argument("--check", action="store_true",
                        help="읽기/전송 상태 확인만 수행 (자동 열기·메시지 발송 없음)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    token = os.environ.get("KANAO_TOKEN") or os.environ.get("NICHIRIN_TOKEN")
    if not token:
        LOG.error("KANAO_TOKEN 또는 NICHIRIN_TOKEN 환경변수가 필요합니다")
        return 2
    client = NichirinClient(args.server, token)
    try:
        registry = Registry(STATE_DIR / "rooms.json")
        manager = Manager(client, registry)
        if args.check:
            room = manager.admin_room()
            print(f"관리 채팅방: {room['title']} (ID {room['id']})")
            print(f"현재 상태: {manager.check_status(room).lines()}")
            print(f"추가 등록된 채팅방: {len(registry.rooms)}개")
            print("점검 모드: 채팅방 자동 열기와 메시지 발송을 하지 않았습니다.")
            return 0
        with single_instance():
            manager.activate_admin()
            LOG.info("관리방 활성화 알림의 전송 요청을 완료했습니다")
            watch(client, manager)
    except KeyboardInterrupt:
        LOG.info("사용자가 채팅방관리봇을 종료했습니다")
    except (NichirinError, ValueError, RuntimeError, OSError) as exc:
        LOG.error("실행 실패: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Nichirin serve (HTTP + SSE) 최소 의존성 클라이언트."""
import json
from collections.abc import Iterable, Iterator
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

    def find_exact_room(self, title: str = "ADMINROOM") -> dict:
        rooms = self._request("/rooms").get("rooms", [])
        matches = [room for room in rooms if room.get("title") == title]
        if not matches:
            raise NichirinError(f"정확히 '{title}'인 채팅방을 찾지 못했습니다")
        if len(matches) != 1:
            raise NichirinError(f"'{title}'이라는 이름의 채팅방이 여러 개여서 실행을 거부합니다")
        room = matches[0]
        if not room.get("readable"):
            raise NichirinError(
                f"'{title}' 채팅방의 대화 DB를 읽을 수 없습니다. "
                "PC 카카오톡에서 해당 방을 한 번 열고 다시 실행하세요 (읽음 처리될 수 있음)."
            )
        return room

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

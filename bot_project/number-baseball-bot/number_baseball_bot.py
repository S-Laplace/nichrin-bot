"""Nichirin 숫자야구 봇. Python 표준 라이브러리만 사용한다."""

import argparse
import json
import logging
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LOG = logging.getLogger("number_baseball")


def create_secret() -> str:
    return "".join(secrets.SystemRandom().sample("123456789", 3))


def score(secret: str, guess: str) -> tuple[int, int]:
    strikes = sum(a == b for a, b in zip(secret, guess))
    balls = sum(digit in secret for digit in guess) - strikes
    return strikes, balls


@dataclass
class Game:
    secret: str
    attempts: int = 0


class BaseballBot:
    HELP = (
        "⚾ 숫자야구 | 도움말\n"
        + "\u200b" * 500
        + "\n\n🎯 게임 규칙\n"
        "1~9 중 중복 없는 숫자 3개를 맞혀보세요.\n\n"
        "📌 명령어\n"
        "• !숫자야구 시작 — 새 게임 시작\n"
        "• !숫자야구 123 — 숫자 추측\n"
        "• !숫자야구 포기 — 게임 포기\n"
        "• !숫자야구도움말 — 도움말 보기\n\n"
        "💡 판정 기준\n"
        "S: 숫자·위치 모두 일치\n"
        "B: 숫자만 일치\n"
        "아웃: 일치하는 숫자가 없음"
    )

    def __init__(self, secret_factory=create_secret):
        self.games: dict[str, Game] = {}
        self.secret_factory = secret_factory

    def handle(self, player_id: str, text: str) -> str | None:
        player_id = str(player_id)
        text = text.strip()
        if text == "!숫자야구도움말":
            return self.HELP
        if not text.startswith("!숫자야구 "):
            return None

        command = text[len("!숫자야구"):].strip()
        if command in ("", "도움말"):
            return None

        if command == "시작":
            secret = self.secret_factory()
            if not re.fullmatch(r"[1-9]{3}", secret) or len(set(secret)) != 3:
                raise ValueError("잘못된 정답 생성")
            self.games[player_id] = Game(secret)
            return (
                "⚾ 숫자야구 시작!\n\n"
                "🎯 1~9 중 서로 다른 숫자 3개를 맞혀보세요.\n"
                "✍️ 입력 예시: !숫자야구 123\n\n"
                "💡 명령어 안내: !숫자야구도움말"
            )

        if command in ("포기", "종료"):
            game = self.games.pop(player_id, None)
            if game is None:
                return "ℹ️ 진행 중인 게임이 없습니다.\n\n▶️ 시작하기: !숫자야구 시작"
            return (
                "🏳️ 숫자야구를 포기했습니다.\n\n"
                f"🔓 정답: {game.secret}\n"
                "🔄 다시 시작: !숫자야구 시작"
            )

        if not re.fullmatch(r"[1-9]{3}", command) or len(set(command)) != 3:
            return (
                "⚠️ 올바른 숫자를 입력해 주세요.\n\n"
                "🎯 1~9 중 중복 없는 숫자 3개\n"
                "✍️ 입력 예시: !숫자야구 123"
            )

        game = self.games.get(player_id)
        if game is None:
            return "⚠️ 아직 시작한 게임이 없습니다.\n\n▶️ 먼저 !숫자야구 시작을 입력하세요."

        game.attempts += 1
        strikes, balls = score(game.secret, command)
        if strikes == 3:
            self.games.pop(player_id)
            return (
                "🎉 정답입니다!\n\n"
                f"🔢 정답: {command}\n"
                f"🏆 {game.attempts}번 만에 성공했습니다.\n\n"
                "🔄 다시 시작: !숫자야구 시작"
            )

        result = "아웃 (0S 0B)" if strikes == 0 and balls == 0 else f"{strikes}S {balls}B"
        return (
            f"🔎 {command} | {game.attempts}번째 시도\n\n"
            f"📊 판정: {result}\n\n"
            f"✍️ 직전 추측: !숫자야구 {command}"
        )


class NichirinError(RuntimeError):
    pass


def iter_sse(lines):
    event = "message"
    data = []
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
            detail = ""
            try:
                payload = json.load(exc)
                error = payload.get("error", {}) if isinstance(payload, dict) else {}
                if isinstance(error, dict):
                    if error.get("code"):
                        detail += f" [{error['code']}]"
                    if error.get("message"):
                        detail += f" {error['message']}"
            except (ValueError, UnicodeDecodeError, OSError):
                # 오류 응답 본문이 비어 있어도 HTTP 상태는 전달한다.
                pass
            raise NichirinError(f"HTTP {exc.code}: {exc.reason}{detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise NichirinError(f"Nichirin 연결 실패: {exc}") from exc
        if not isinstance(result, dict) or not result.get("ok"):
            raise NichirinError(f"Nichirin 오류: {result!r}")
        return result

    def send(self, room_id: str, text: str) -> dict:
        result = self._request("/send", {
            "room": room_id,
            "text": text,
            "openRoom": True,
        })
        if not result.get("submitted"):
            raise NichirinError("메시지 입력 성공 여부를 확인할 수 없습니다")
        return result

    def stream(self, cursor: dict[str, str] | None = None):
        query = ""
        if cursor:
            query = "?" + urlencode([
                ("since", f"{room}:{message_id}")
                for room, message_id in cursor.items()
            ])
        request = Request(
            self.base_url + "/stream" + query,
            headers={**self.headers, "Accept": "text/event-stream"},
        )
        try:
            with urlopen(request, timeout=90) as response:
                yield from iter_sse(response)
        except HTTPError as exc:
            raise NichirinError(f"SSE HTTP {exc.code}: {exc.reason}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise NichirinError(f"SSE 연결 실패: {exc}") from exc


def is_target_message(message: dict) -> bool:
    return (
        bool(message.get("roomId"))
        and message.get("fromMe") is False
        and message.get("kind") in ("text", "reply")
        and bool(message.get("senderId"))
        and isinstance(message.get("text"), str)
    )


def dispatch(client: NichirinClient, bot: BaseballBot, message: dict) -> bool:
    if not is_target_message(message):
        return False
    room_id = str(message["roomId"])
    player_id = f"{room_id}:{message['senderId']}"
    response = bot.handle(player_id, message["text"])
    if response is None:
        return False
    client.send(room_id, response)
    return True


def run(client: NichirinClient) -> None:
    bot = BaseballBot()
    cursor: dict[str, str] = {}
    delay = 1
    LOG.info("모든 복호화 가능 채팅방 감시 시작. Ctrl+C로 종료합니다.")

    while True:
        try:
            for event, payload in client.stream(cursor):
                if event == "error":
                    raise NichirinError(f"서버가 SSE 오류를 알렸습니다: {payload.get('error')}")
                if event != "message" or not payload.get("ok"):
                    continue

                message = payload.get("message")
                if not isinstance(message, dict):
                    continue
                room_id = str(message.get("roomId") or "")
                if not room_id:
                    continue
                message_id = str(message.get("id", ""))
                if not message_id.isdigit():
                    LOG.warning("유효하지 않은 메시지 ID가 있어 건너뜁니다")
                    continue
                previous = cursor.get(room_id)
                if previous is not None and int(message_id) <= int(previous):
                    continue

                # 송신 성공 여부가 불명확해도 같은 명령을 다시 전송하지 않는다.
                cursor[room_id] = message_id
                if dispatch(client, bot, message):
                    LOG.info("숫자야구 응답 요청 완료: 방=%s 메시지 ID=%s", room_id, message_id)
                delay = 1

            LOG.warning("SSE 연결이 종료되었습니다. 재연결합니다.")
        except (NichirinError, ValueError) as exc:
            LOG.warning("연결/처리 문제: %s", exc)
        time.sleep(delay)
        delay = min(delay * 2, 30)


def main() -> int:
    parser = argparse.ArgumentParser(description="Nichirin 숫자야구 봇")
    parser.add_argument(
        "--server",
        default=os.environ.get("KANAO_URL", "").strip() or "http://127.0.0.1:8765",
        help="Nichirin 서버 주소 (기본: KANAO_URL 또는 127.0.0.1:8765)",
    )
    parser.add_argument("--check", action="store_true", help="서버 및 읽기 가능 채팅방 확인")
    args = parser.parse_args()

    token = os.environ.get("KANAO_TOKEN") or os.environ.get("NICHIRIN_TOKEN")
    if not token:
        print("환경변수 KANAO_TOKEN 또는 NICHIRIN_TOKEN을 설정하세요.", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    client = NichirinClient(args.server, token)
    if args.check:
        try:
            rooms = client._request("/rooms").get("rooms", [])
        except NichirinError as exc:
            LOG.error("서버 확인 실패: %s", exc)
            return 1
        readable = sum(bool(room.get("readable")) for room in rooms if isinstance(room, dict))
        print(f"서버 연결 성공: 복호화 가능한 채팅방 {readable}개")
        return 0

    try:
        run(client)
    except KeyboardInterrupt:
        LOG.info("사용자가 종료했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

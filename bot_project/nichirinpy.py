"""Nichirin 봇 통합 실행기: 한 명령으로 네 봇과 공용 서버를 관리합니다."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from nichirin_client import NichirinClient, NichirinError

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
BOTS = {
    "baseball": ("number-baseball-bot", "number_baseball_bot.py", "숫자야구"),
    "summary": ("api-base-summary-bot", "api_summary_bot.py", "AI 요약"),
    "reserve": ("reserved-message-send-system", "reserved_message_bot.py", "예약 발송"),
    "manager": ("chat-room-management-bot", "chat_room_management_bot.py", "채팅방 관리"),
}


def selected_bots(names: str) -> list[str]:
    parts = [name.strip().lower() for name in names.split(",")]
    if parts == ["all"]:
        return list(BOTS)
    if not parts or any(name not in BOTS for name in parts):
        raise ValueError("봇 이름은 " + ", ".join(BOTS) + " 또는 all을 사용하세요.")
    if len(set(parts)) != len(parts):
        raise ValueError("봇 이름을 중복 지정할 수 없습니다.")
    return parts


def command_for(name: str, url: str) -> list[str]:
    directory, filename, _ = BOTS[name]
    command = [sys.executable, "-u", str(HERE / directory / filename)]
    if name == "baseball":
        command += ["--server", url]
    elif name == "summary":
        command.append("--from-now")
    elif name == "reserve":
        command.append("--live")
    elif name == "manager":
        command += ["--server", url]
    return command


def configured_url(port: int, environ: dict[str, str]) -> str:
    return environ.get("KANAO_URL", "").strip().rstrip("/") or f"http://127.0.0.1:{port}"


def select_server_url(port: int, environ: dict[str, str]) -> str:
    """기존 기본 서버의 토큰을 모르면 별도 빈 포트를 사용합니다."""
    requested = configured_url(port, environ)
    if environ.get("KANAO_URL") or environ.get("KANAO_TOKEN") or not listening(requested):
        return requested
    for candidate in range(port + 1, min(port + 21, 65536)):
        alternative = f"http://127.0.0.1:{candidate}"
        if not listening(alternative):
            print(f"포트 {port}가 기존 서버에서 사용 중이라 {candidate} 포트를 사용합니다.")
            return alternative
    raise RuntimeError("사용 가능한 Nichirin 포트가 없습니다 (다음 20개 포트 확인).")


def listening(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("KANAO_URL은 http(s) 주소여야 합니다.")
    with socket.socket() as probe:
        probe.settimeout(0.7)
        return probe.connect_ex((parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))) == 0


def verify_server(url: str, token: str) -> int:
    if not token:
        raise RuntimeError("기존 서버에 연결하려면 KANAO_TOKEN 환경변수가 필요합니다.")
    rooms = NichirinClient(url, token)._request("/rooms").get("rooms", [])
    return len(rooms)


def start_server(url: str, environ: dict[str, str]) -> subprocess.Popen:
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost") or parts.path:
        raise RuntimeError("새 서버 자동 실행은 로컬 http://127.0.0.1:포트에서만 지원합니다.")
    port = parts.port or 80
    if not (1024 <= port <= 65535):
        raise RuntimeError("서버 포트는 1024~65535 범위여야 합니다.")
    environ.setdefault("KANAO_TOKEN", secrets.token_urlsafe(36))
    environ.setdefault("KANAO_ROOM_LIST_LOGICAL_COORDS", "1")
    logs = HERE / "data"
    logs.mkdir(exist_ok=True)
    with (logs / "nichirinpy-server.log").open("a", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "nichirin", "serve", "--port", str(port)],
            cwd=PROJECT, env=environ, stdout=output, stderr=subprocess.STDOUT,
        )
    for _ in range(60):
        if process.poll() is not None:
            break
        try:
            with urlopen(url + "/health", timeout=0.7) as response:
                if response.status == 200:
                    return process
        except OSError:
            time.sleep(0.3)
    stop_process(process)
    raise RuntimeError("Nichirin 서버 시작 실패: bot_project/data/nichirinpy-server.log를 확인하세요.")


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@contextmanager
def single_launcher():
    """실수로 통합 실행기를 중복 가동하는 것을 방지합니다."""
    folder = HERE / "data"
    folder.mkdir(exist_ok=True)
    with (folder / "nichirinpy.lock").open("a+b") as handle:
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
                raise RuntimeError("nichirinpy.py가 이미 실행 중입니다.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def running_standalone_bots(chosen: list[str]) -> list[str]:
    """Windows에서 이미 실행 중인 개별 봇 프로세스를 보수적으로 검사합니다."""
    if os.name != "nt":
        return []
    query = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -in @('python.exe','pythonw.exe') } | "
        "ForEach-Object { $_.CommandLine }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", query],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("기존 봇 프로세스 검사에 실패했습니다.") from exc
    if result.returncode:
        raise RuntimeError("기존 봇 프로세스 목록을 확인할 수 없습니다.")
    lines = [line.lower().replace("/", "\\") for line in result.stdout.splitlines()]
    markers = {
        "baseball": ("number_baseball_bot.py",),
        "summary": ("api_summary_bot.py",),
        "reserve": ("reserved_message_bot.py", "reserved-message-send-system\\app.py"),
        "manager": ("chat_room_management_bot.py",),
    }
    return [name for name in chosen
            if any(marker in line for marker in markers[name] for line in lines)]


def run_all(chosen: list[str], url: str, environ: dict[str, str]) -> int:
    owned_server = None
    children: list[tuple[str, subprocess.Popen]] = []
    try:
        if listening(url):
            count = verify_server(url, environ.get("KANAO_TOKEN", ""))
            print(f"기존 Nichirin 서버 연결: {url} (채팅방 {count}개)", flush=True)
        else:
            owned_server = start_server(url, environ)
            count = verify_server(url, environ["KANAO_TOKEN"])
            print(f"공용 Nichirin 서버 시작: {url} (채팅방 {count}개)", flush=True)
        environ["KANAO_URL"] = url
        for name in chosen:
            directory, _, label = BOTS[name]
            command = command_for(name, url)
            process = subprocess.Popen(command, cwd=HERE / directory, env=environ)
            children.append((name, process))
            print(f"[시작] {label} PID={process.pid}", flush=True)
        print("통합 실행 중. Ctrl+C를 누르면 이 실행기가 시작한 봇들이 종료됩니다.", flush=True)
        while True:
            for name, process in children:
                code = process.poll()
                if code is not None:
                    print(f"[종료 감지] {BOTS[name][2]} (코드 {code}): 다른 봇도 종료합니다.", file=sys.stderr)
                    return code or 1
            if owned_server and owned_server.poll() is not None:
                print("공용 Nichirin 서버가 종료되었습니다.", file=sys.stderr)
                return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n통합 실행기를 종료합니다.", flush=True)
        return 0
    finally:
        for _, process in reversed(children):
            stop_process(process)
        if owned_server is not None:
            stop_process(owned_server)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nichirin 봇 통합 실행기")
    parser.add_argument("--bots", default="all", metavar="NAMES",
                        help="all 또는 baseball,summary,reserve,manager (쉼표로 구분)")
    parser.add_argument("--list", action="store_true", help="지원하는 봇 목록만 표시")
    parser.add_argument("--check", action="store_true", help="환경과 서버 점검만 수행 (실제 실행 없음)")
    parser.add_argument("--server-port", type=int, default=8765, help="자동 서버 기본 포트 (기본 8765)")
    args = parser.parse_args(argv)
    if args.list:
        for key, (_, filename, label) in BOTS.items():
            print(f"{key:9} {label} ({filename})")
        return 0
    try:
        chosen = selected_bots(args.bots)
        if not 1024 <= args.server_port <= 65535:
            raise ValueError("--server-port는 1024~65535 범위여야 합니다.")
        for name in chosen:
            directory, filename, _ = BOTS[name]
            if not (HERE / directory / filename).is_file():
                raise RuntimeError(f"실행 파일이 없습니다: {directory}/{filename}")
        environ = os.environ.copy()
        token = environ.get("KANAO_TOKEN") or environ.get("NICHIRIN_TOKEN")
        if token:
            environ["KANAO_TOKEN"] = token
        url = select_server_url(args.server_port, environ)
        if args.check:
            print("선택된 봇:", ", ".join(chosen))
            if listening(url):
                count = verify_server(url, environ.get("KANAO_TOKEN", ""))
                print(f"Nichirin 연결 OK: {url} (채팅방 {count}개)")
            else:
                print(f"Nichirin 서버 미실행: {url} (정상 실행 시 자동 시작)")
            print("점검 종료: 봇 시작, 채팅방 열기, 메시지 발송 없음")
            return 0
        already_running = running_standalone_bots(chosen)
        if already_running:
            raise RuntimeError("기존 개별 봇이 실행 중입니다: "
                               + ", ".join(already_running)
                               + ". 중복 발송 방지를 위해 먼저 종료하세요.")
        with single_launcher():
            return run_all(chosen, url, environ)
    except (OSError, RuntimeError, ValueError, NichirinError) as exc:
        print(f"실행 불가: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

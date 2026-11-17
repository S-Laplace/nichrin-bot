# nichirin-project

[Nichirin](https://github.com/sanguneo/nichirin)을 바탕으로 카카오톡에서 쓸 봇 네 개를 만들고, Windows에서 사용하면서 필요한 부분을 수정했습니다. 원본 Nichirin 소스도 함께 들어 있습니다. 제가 추가한 봇은 `bot_project/`에 모아 뒀습니다.

> Windows용 카카오톡 PC 앱을 사용하는 비공식 프로젝트입니다. 카카오에서 제공하거나 승인한 프로그램이 아니며, 사용에 따른 계정 제한과 데이터 취급 문제는 직접 확인해야 합니다.

## 만든 봇

| 봇 | 하는 일 | 주요 명령어 |
| --- | --- | --- |
| [숫자야구](bot_project/number-baseball-bot/number_baseball_bot.py) | 채팅방에서 1~9 중 서로 다른 숫자 세 개를 맞히는 게임 | `!숫자야구 시작`, `!숫자야구 123`, `!숫자야구 포기`, `!숫자야구도움말` |
| [AI 대화 요약](bot_project/api-base-summary-bot/api_summary_bot.py) | 채팅방의 지난 요약 지점 이후 대화를 Google Gemini API로 요약 | `!요약`, `!요약도움말` |
| [예약 발송](bot_project/reserved-message-send-system/reserved_message_bot.py) | 채팅방별 메시지를 정해진 시각에 보내고 반복 예약을 관리 | `!예약 추가`, `!예약 목록`, `!예약 삭제 1`, `!예약도움말` |
| [채팅방 관리](bot_project/chat-room-management-bot/chat_room_management_bot.py) | 지정한 관리자 채팅방에서 다른 채팅방의 읽기·전송 상태를 확인하고 활성화 | `!방등록`, `!방목록`, `!방상태`, `!방활성화`, `!방삭제`, `!방전체활성화`, `!채팅방관리도움말` |

요약 봇은 **처음 `!요약`을 입력하면 기준점을 등록**합니다. 다음 `!요약`부터 직전 요약 명령 이후의 대화를 대상으로 합니다. 읽을 수 없는 대화나 조회 범위를 초과한 대화를 임의로 생략해서 요약하지 않습니다.

예약은 **1회·매일·매주·매월**을 지원하고, 한국 시간(KST)을 기준으로 처리합니다. 채팅방 구성원은 자신이 만든 예약을 목록에서 확인하거나 삭제할 수 있습니다. 예를 들어 `!예약 추가 2027-01-15 18:30 1회 회의 시작`처럼 입력합니다. 예약 관리 페이지는 실행 중인 PC의 `http://127.0.0.1:8848/`에서 열 수 있습니다.

채팅방 관리 명령어는 설정한 **관리자 채팅방에서만** 처리합니다. 자세한 명령어는 카카오톡에서 각 봇의 도움말 명령어를 입력하면 확인할 수 있습니다.

## 내려받아 실행하기

**준비물:** Windows 10/11 PC, Python 3.10 이상, 설치 및 로그인된 카카오톡 PC 앱. 봇 실행에 별도 Python 패키지를 설치할 필요는 없습니다. AI 요약에는 Google AI Studio API 키와 인터넷 연결이 추가로 필요합니다.

1. GitHub 페이지에서 **Code → Download ZIP**으로 내려받아 압축을 풉니다. Git을 사용한다면 이 저장소의 HTTPS 주소로 clone해도 됩니다.
2. 압축을 푼 프로젝트 최상위 폴더에서 PowerShell 또는 터미널을 엽니다.
3. 모든 봇을 사용할 계획이라면 아래 두 가지를 먼저 설정합니다.
   - `bot_project/api-base-summary-bot/api_summary_bot.py`의 `GEMINI_API_KEY = "YOURAPIKEY"`를 **본인의 API 키**로 변경합니다. 모델이 사용 불가로 표시되면 같은 파일의 `GEMINI_MODEL`을 계정에서 사용 가능한 모델로 바꿉니다.
   - `bot_project/chat-room-management-bot/chat_room_management_bot.py`의 `ADMIN_ROOM_TITLE = "ADMINROOM"`을 실제 관리자 채팅방 이름으로 변경합니다. 이름은 정확히 일치해야 합니다.
4. 다음 명령으로 실행합니다.

```powershell
python --version
python bot_project/nichirinpy.py --list
python bot_project/nichirinpy.py --check
python bot_project/nichirinpy.py
```

`nichirinpy.py`는 공용 Nichirin 서버와 선택한 봇을 함께 실행합니다. 종료하려면 실행한 터미널에서 **Ctrl+C**를 누릅니다. 새 서버를 자동으로 시작하는 기본 실행에는 별도 서버 토큰을 미리 설정할 필요가 없습니다. 이미 실행 중인 서버에 연결할 때는 해당 서버의 `KANAO_TOKEN`이 필요합니다.

설정을 끝내지 않았다면 필요한 봇만 선택해 실행할 수 있습니다.

```powershell
# API 키와 관리자 채팅방 설정 없이 실행
python bot_project/nichirinpy.py --bots baseball,reserve

# 요약 API 키만 설정했고 채팅방 관리 봇은 제외할 때
python bot_project/nichirinpy.py --bots baseball,summary
```

`--bots`에는 `baseball`, `summary`, `reserve`, `manager`를 쉼표로 구분해 지정합니다. 한 번에 하나의 통합 실행기만 사용하고, 동일한 봇을 개별 실행 파일로 동시에 중복 실행하지 않는 편이 안전합니다.

카카오톡이 실행 중이고 로그인되어 있어야 메시지를 읽고 보낼 수 있습니다. 일부 채팅방은 복호화 키를 확보하기 위해 해당 방을 열어야 할 수 있으며, 창 전환이 필요한 기능은 카카오톡 화면 상태에 영향을 받습니다. 봇이 반응하지 않으면 먼저 `--check`로 연결 상태를 확인합니다.

**API 키 주의:** 요약 봇은 API 키를 소스 파일에 직접 적는 방식입니다. 자신의 키를 입력한 파일을 그대로 GitHub에 올리거나 다른 사람에게 공유하지 마세요. 공개 전에는 반드시 `YOURAPIKEY`로 되돌려야 합니다. 예약 데이터·로그·채팅 관련 로컬 데이터는 Git에 포함하지 않는 것을 전제로 합니다.

## 원본 Nichirin에서 수정한 부분

봇이 안정적으로 동작하도록 원본 [sanguneo/nichirin](https://github.com/sanguneo/nichirin)의 **Windows 쪽 코드 일부**도 수정했습니다. 여기 적은 차이는 이 저장소가 기반으로 삼은 원본 코드와 비교한 것입니다.

| 파일 | 수정한 내용 |
| --- | --- |
| `windows/kanao/serve/backends.py` · `windows/kanao/cli.py` | SSE 감시 중 복호화 키를 주기적으로 다시 찾고, 새 키를 HTTP 읽기 기능과 공유하도록 연결했습니다. 일시적인 검색 실패로 기존 키를 잃지 않도록 했습니다. |
| `windows/kanao/watch.py` | 복호화가 뒤늦게 가능해진 채팅방의 최근 메시지를 제한된 범위에서 보완해 첫 명령을 놓칠 가능성을 줄였습니다. |
| `windows/kanao/paths.py` | 카카오톡 사용자 프로필이 여럿일 때 실행 중인 클라이언트의 복호화 키와 DB 정보를 비교해 프로필을 선택하도록 수정했습니다. |
| `windows/kanao/winauto.py` | 채팅방 검색 전에 채팅 탭으로 전환하고, Windows 화면 배율에 따른 좌표 처리 문제를 보정했습니다. |

관련 테스트 코드는 `windows/tests/`에 포함되어 있습니다. **macOS 코드는 별도로 수정하지 않았습니다.** 원본 Nichirin 자체의 기능과 상세 사용법은 [원본 README](https://github.com/sanguneo/nichirin) 및 [Windows 문서](windows/README.md), [macOS 문서](macos/README.md)를 참고하면 됩니다.

## 라이선스

이 저장소는 **MIT License**로 공개합니다. 제가 추가한 봇 코드의 라이선스는 루트의 [LICENSE](LICENSE)를 따릅니다. 원본에서 가져온 소스의 저작권과 고지 사항은 그대로 유지하며, [Windows 라이선스](windows/LICENSE)와 [macOS 라이선스](macos/LICENSE)에서도 확인할 수 있습니다.

카카오톡을 자동으로 읽거나 조작하는 기능은 공식 API가 아닙니다. 사용하기 전에 [Windows 면책 고지](windows/DISCLAIMER.md)를 확인해 주세요.

## AI Assist

개발 과정에서 일부 코드 작성과 수정, 오류 점검, 문서 정리에 AI 도구를 활용했습니다. 기능 설명과 실행 방법은 이 저장소에 포함된 실제 코드를 기준으로 정리했습니다.

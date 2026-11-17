"""독립 실행 파일에 포함한 Nichirin HTTP/SSE 통신 테스트."""

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api-base-summary-bot"))
import api_summary_bot as bot


class TransportTests(unittest.TestCase):
    def test_parse_sse(self):
        stream = [
            b": keepalive\n",
            b"event: message\n",
            b'data: {"ok": true, "message": {"id": "7"}}\n',
            b"\n",
        ]
        self.assertEqual(list(bot.iter_sse(stream)), [
            ("message", {"ok": True, "message": {"id": "7"}})
        ])

    def test_send_uses_open_room(self):
        response = io.BytesIO(b'{"ok":true,"submitted":true}')
        with patch.object(bot, "urlopen", return_value=response) as open_url:
            result = bot.NichirinClient("http://localhost:8765", "token").send("42", "test")
        self.assertTrue(result["submitted"])
        req = open_url.call_args.args[0]
        self.assertEqual(req.full_url, "http://localhost:8765/send")
        self.assertEqual(req.get_header("Authorization"), "Bearer token")
        self.assertEqual(json.loads(req.data), {
            "room": "42", "text": "test", "openRoom": True,
        })

    def test_unsubmitted_reply_is_an_error(self):
        response = io.BytesIO(b'{"ok":true,"submitted":false}')
        with patch.object(bot, "urlopen", return_value=response):
            with self.assertRaisesRegex(bot.NichirinError, "성공 여부"):
                bot.NichirinClient("http://localhost:8765", "token").send("42", "test")

    def test_missing_token_rejected(self):
        with self.assertRaises(ValueError):
            bot.NichirinClient("http://localhost:8765", "")


if __name__ == "__main__":
    unittest.main()

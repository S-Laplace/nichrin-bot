import io
import json
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "number-baseball-bot"))
from urllib.error import HTTPError

from number_baseball_bot import NichirinClient, NichirinError


class HttpErrorTests(unittest.TestCase):
    def test_404_displays_nichirin_error_code(self):
        payload = {
            "ok": False,
            "error": {
                "code": "WINDOW_NOT_FOUND",
                "message": "No open chat window matches '샘플방'",
            },
        }
        error = HTTPError(
            "http://127.0.0.1:8765/send",
            404,
            "Not Found",
            {},
            io.BytesIO(json.dumps(payload).encode("utf-8")),
        )
        client = NichirinClient("http://127.0.0.1:8765", "test-token")
        with patch("number_baseball_bot.urlopen", side_effect=error):
            with self.assertRaises(NichirinError) as ctx:
                client.send("452470254692244", "pong")
        self.assertIn("WINDOW_NOT_FOUND", str(ctx.exception))
        self.assertIn("샘플방", str(ctx.exception))
        self.assertIn("HTTP 404", str(ctx.exception))


    def test_send_uses_room_id_and_open_room(self):
        client = NichirinClient("http://127.0.0.1:8765", "test-token")
        result = {"ok": True, "submitted": True}
        with patch.object(client, "_request", return_value=result) as request:
            self.assertEqual(client.send("12345", "test"), result)
        request.assert_called_once_with("/send", {
            "room": "12345", "text": "test", "openRoom": True,
        })

    def test_sse_cursor_keeps_separate_rooms(self):
        from urllib.parse import parse_qs, urlsplit

        client = NichirinClient("http://127.0.0.1:8765", "test-token")
        body = b'event: message\n' + b'data: {"ok":true}\n\n'
        with patch("number_baseball_bot.urlopen", return_value=io.BytesIO(body)) as opener:
            self.assertEqual(list(client.stream({"100": "5", "200": "7"})),
                             [("message", {"ok": True})])
        query = parse_qs(urlsplit(opener.call_args.args[0].full_url).query)
        self.assertEqual(query["since"], ["100:5", "200:7"])


if __name__ == "__main__":
    unittest.main()

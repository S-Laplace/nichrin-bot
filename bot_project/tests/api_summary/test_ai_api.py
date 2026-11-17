import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api-base-summary-bot"))
import api_summary_bot as ai_api


class AITests(unittest.TestCase):
    def test_one_batch(self):
        messages = [{"sender": "A", "text": "회의는 내일", "sentAt": "10:00"}]
        with patch.object(ai_api, "_call_ai", return_value="내일 회의") as call:
            self.assertEqual(ai_api.summarize(messages), "내일 회의")
            self.assertEqual(call.call_count, 1)

    def test_gemini_request_and_response(self):
        payload = {"candidates": [{"content": {"parts": [
            {"text": "✅ 내일 회의 확정"}, {"text": "📌 발표 자료 준비"}
        ]}}]}
        fake_response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        with patch.object(ai_api, "GEMINI_API_KEY", "dummy-test-key"), \
             patch.object(ai_api, "urlopen", return_value=fake_response) as call:
            result = ai_api._call_ai("내일 회의 메시지")
        self.assertEqual(result, "✅ 내일 회의 확정\n📌 발표 자료 준비")
        request = call.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://generativelanguage.googleapis.com/v1beta/models/"
                         "gemini-3.5-flash:generateContent")
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["x-goog-api-key"], "dummy-test-key")
        self.assertNotIn("authorization", headers)
        body = json.loads(request.data)
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], ai_api.SYSTEM)
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "내일 회의 메시지")
        self.assertEqual(body["generationConfig"]["thinkingConfig"]["thinkingLevel"], "low")
        self.assertNotIn("temperature", body["generationConfig"])
        self.assertNotIn("thinkingBudget", body["generationConfig"]["thinkingConfig"])

    def test_model_not_available_error(self):
        err = HTTPError("https://generativelanguage.googleapis.com", 404,
                        "Not Found", {}, None)
        with patch.object(ai_api, "urlopen", side_effect=err):
            with self.assertRaisesRegex(ai_api.AIError, "사용할 수 없습니다\\(404\\)"):
                ai_api._call_ai("테스트")

    def test_gemini_rate_limit(self):
        err = HTTPError("https://generativelanguage.googleapis.com", 429,
                        "Too Many Requests", {}, None)
        with patch.object(ai_api, "urlopen", side_effect=err):
            with self.assertRaisesRegex(ai_api.AIError, "요청 한도"):
                ai_api._call_ai("테스트")

    def test_gemini_empty_response(self):
        payload = {"candidates": [{"content": {"parts": []}}]}
        fake_response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        with patch.object(ai_api, "urlopen", return_value=fake_response):
            with self.assertRaisesRegex(ai_api.AIError, "요약 텍스트"):
                ai_api._call_ai("테스트")

    def test_gemini_blocked_response(self):
        payload = {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
        fake_response = io.BytesIO(json.dumps(payload).encode("utf-8"))
        with patch.object(ai_api, "urlopen", return_value=fake_response):
            with self.assertRaisesRegex(ai_api.AIError, "차단"):
                ai_api._call_ai("테스트")

    def test_empty(self):
        with patch.object(ai_api, "_call_ai") as call:
            self.assertEqual(ai_api.summarize([]), "요약할 대화가 없습니다.")
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()


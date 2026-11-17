"""SSE 커서의 영구 저장과 중복 방지 테스트."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api-base-summary-bot"))
from api_summary_bot import CursorState


class CursorTests(unittest.TestCase):
    def test_cursor_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cursors.json"
            state = CursorState(path)
            self.assertTrue(state.advance("100", "55"))
            self.assertFalse(state.advance("100", "55"))
            self.assertFalse(state.advance("100", "54"))
            self.assertEqual(CursorState(path).cursors, {"100": "55"})


if __name__ == "__main__":
    unittest.main()

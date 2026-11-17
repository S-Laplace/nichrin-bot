"""ID-boundary selection tests: no real KakaoTalk or AI calls."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api-base-summary-bot"))
import api_summary_bot as run
from api_summary_bot import SelectionError, SummaryBoundaryState, select_since_command


def msg(n, text=None, *, own=False, kind="text", room="100"):
    return {"id": str(n), "roomId": room, "senderId": "42",
            "fromMe": own, "kind": kind, "text": text if text is not None else f"대화 {n}"}


def select(items, command_id, previous=None, **kwargs):
    items = sorted(items, key=lambda row: int(row["id"]))
    command = next(row for row in items if int(row["id"]) == command_id)
    def page(room_id, before_id, limit):
        return [row for row in items if int(row["id"]) < before_id][-limit:]
    return select_since_command("100", command, previous, page, **kwargs)


class RangeTests(unittest.TestCase):
    def test_find_previous_command_even_after_read(self):
        items = [msg(1, "!요약"), msg(2), msg(3, own=True),
                 msg(4, kind="feed"), msg(5, "!요약도움말"),
                 msg(6), msg(7, "!요약"), msg(8)]
        selected, previous = select(items, 7, page_size=2)
        self.assertEqual(previous, "1")
        self.assertEqual([r["id"] for r in selected], ["2", "6"])

    def test_saved_boundary_overrides_unprocessed_prior_commands(self):
        items = [msg(1, "!요약"), msg(2), msg(3, "!요약"),
                 msg(4), msg(5, "!요약")]
        selected, previous = select(items, 5, "1", page_size=2)
        self.assertEqual(previous, "1")
        self.assertEqual([r["id"] for r in selected], ["2", "4"])

    def test_first_command_initializes(self):
        selected, previous = select([msg(1), msg(2, "!요약")], 2)
        self.assertEqual((selected, previous), ([], None))

    def test_no_changes_between_commands(self):
        selected, previous = select([msg(1, "!요약"), msg(2, "!요약")], 2, "1")
        self.assertEqual((selected, previous), ([], "1"))

    def test_limit_never_returns_partial_history(self):
        items = [msg(1, "!요약")] + [msg(i) for i in range(2, 9)] + [msg(9, "!요약")]
        with self.assertRaisesRegex(SelectionError, "초과"):
            select(items, 9, "1", max_scan_rows=3, page_size=2)

    def test_invalid_saved_boundary_is_rejected(self):
        with self.assertRaises(SelectionError):
            select([msg(4, "!요약")], 4, "4")

    def test_missing_saved_boundary_is_rejected(self):
        with self.assertRaisesRegex(SelectionError, "기록"):
            select([msg(4, "대화"), msg(5, "!요약")], 5, "1")

    def test_pagination_duplicate_rejected(self):
        command = msg(5, "!요약")
        def broken(room, before, size):
            return [msg(3), msg(3)]
        with self.assertRaisesRegex(SelectionError, "중복"):
            select_since_command("100", command, "1", broken, page_size=3)


class BoundaryTests(unittest.TestCase):
    def test_persistence_and_room_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "data" / "boundaries.json"
            state = SummaryBoundaryState(p)
            self.assertTrue(state.advance("100", "999"))
            self.assertTrue(state.advance("200", "888"))
            self.assertFalse(state.advance("100", "998"))
            self.assertFalse(state.advance("100", "999"))
            new_state = SummaryBoundaryState(p)
            self.assertEqual(new_state.get("100"), "999")
            self.assertEqual(new_state.get("200"), "888")
            self.assertIsNone(new_state.get("300"))

    def test_malformed_file_fails_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "boundaries.json"
            p.write_text('{"100":"oops"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                SummaryBoundaryState(p)

    def test_failure_does_not_advance(self):
        class Client:
            def __init__(self):
                self.sent = []
            def _request(self, url):
                return {"rooms": [{"id": "100", "readable": True, "unread": 0}]}
            def send(self, room, text):
                self.sent.append((room, text))
                return {"submitted": True}

        with tempfile.TemporaryDirectory() as tmp:
            boundaries = SummaryBoundaryState(Path(tmp) / "b.json")
            boundaries.advance("100", "1")
            client = Client()
            rows = [msg(1, "!요약"), msg(2), msg(3, "!요약")]
            def page(room, before, limit):
                return [r for r in rows if int(r["id"]) < before][-limit:]
            with patch.object(run, "fetch_page", side_effect=page), \
                 patch.object(run, "summarize", side_effect=run.AIError("API 실패")):
                run.handle_command(client, rows[-1], boundaries)
            self.assertEqual(boundaries.get("100"), "1")
            self.assertIn("API 실패", client.sent[-1][1])


if __name__ == "__main__":
    unittest.main()

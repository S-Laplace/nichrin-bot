"""통합 실행기의 부작용 없는 검증."""
import contextlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nichirinpy


class UnifiedLauncherTests(unittest.TestCase):
    def test_all_bots_selected(self):
        self.assertEqual(nichirinpy.selected_bots("all"), list(nichirinpy.BOTS))

    def test_custom_bots_selected(self):
        self.assertEqual(nichirinpy.selected_bots("summary,baseball"), ["summary", "baseball"])

    def test_unknown_bot_is_rejected(self):
        with self.assertRaises(ValueError):
            nichirinpy.selected_bots("unknown")

    def test_duplicate_bot_is_rejected(self):
        with self.assertRaises(ValueError):
            nichirinpy.selected_bots("manager,manager")

    def test_each_launcher_points_to_existing_file(self):
        for name in nichirinpy.BOTS:
            command = nichirinpy.command_for(name, "http://127.0.0.1:8766")
            self.assertTrue(Path(command[2]).is_file(), name)

    def test_commands_pass_expected_modes(self):
        url = "http://127.0.0.1:8766"
        self.assertIn("--from-now", nichirinpy.command_for("summary", url))
        self.assertIn("--live", nichirinpy.command_for("reserve", url))
        for name in ("baseball", "manager"):
            command = nichirinpy.command_for(name, url)
            self.assertEqual(command[-2:], ["--server", url])

    def test_custom_server_url(self):
        self.assertEqual(nichirinpy.configured_url(8765, {"KANAO_URL": "http://127.0.0.1:9999/"}),
                         "http://127.0.0.1:9999")

    @patch.object(nichirinpy, "listening", side_effect=[True, False])
    def test_busy_default_port_uses_available_port(self, fake_listening):
        with contextlib.redirect_stdout(io.StringIO()):
            result = nichirinpy.select_server_url(8765, {})
        self.assertEqual(result, "http://127.0.0.1:8766")
        self.assertEqual(fake_listening.call_count, 2)

    @patch.object(nichirinpy, "listening")
    def test_explicit_server_is_not_replaced(self, fake_listening):
        result = nichirinpy.select_server_url(8765, {"KANAO_URL": "http://127.0.0.1:9000"})
        self.assertEqual(result, "http://127.0.0.1:9000")
        fake_listening.assert_not_called()

    @patch.object(nichirinpy, "running_standalone_bots", return_value=["manager"])
    @patch.object(nichirinpy, "run_all")
    @patch.object(nichirinpy, "select_server_url", return_value="http://127.0.0.1:8766")
    def test_duplicate_standalone_bot_blocks_launch(self, _, mock_run, __):
        with patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(nichirinpy.main(["--bots", "manager"]), 1)
        mock_run.assert_not_called()

    @patch.object(nichirinpy, "stop_process")
    @patch.object(nichirinpy, "verify_server", return_value=1)
    @patch.object(nichirinpy, "listening", return_value=False)
    @patch.object(nichirinpy, "start_server")
    @patch.object(nichirinpy.subprocess, "Popen")
    def test_child_failure_cleans_up_server(self, fake_popen, fake_server, _, __, stop):
        child = fake_popen.return_value
        child.pid = 100
        child.poll.return_value = 2
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(nichirinpy.run_all(
                    ["baseball"], "http://127.0.0.1:8766", {"KANAO_TOKEN": "test-token"}), 2)
        stop.assert_any_call(child)
        stop.assert_any_call(fake_server.return_value)

    def test_list_has_no_server_side_effects(self):
        with patch.object(nichirinpy, "run_all") as mock_run:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(nichirinpy.main(["--list"]), 0)
            mock_run.assert_not_called()

    @patch.object(nichirinpy, "listening", return_value=False)
    @patch.object(nichirinpy, "run_all")
    def test_check_never_starts_bots(self, mock_run, _):
        with patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(nichirinpy.main(["--check", "--bots", "summary"]), 0)
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

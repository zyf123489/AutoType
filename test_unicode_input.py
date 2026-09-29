import ctypes
import queue
import threading
import unittest
from unittest.mock import MagicMock, call, patch

import main
import unicode_input


class UnicodeInputTests(unittest.TestCase):
    def test_sends_utf16_key_pairs_for_bmp_and_supplementary_characters(self):
        captured = []

        def capture(count, events, size):
            self.assertEqual(size, ctypes.sizeof(unicode_input.INPUT))
            captured.extend(
                (events[index].type, events[index].data.ki.wScan, events[index].data.ki.dwFlags)
                for index in range(count)
            )
            return count

        with patch.object(unicode_input, "_send_input", side_effect=capture):
            unicode_input.send_unicode_char("中")
            unicode_input.send_unicode_char("😀")

        expected = []
        for unit in (0x4E2D, 0xD83D, 0xDE00):
            expected.extend(
                [
                    (1, unit, unicode_input.KEYEVENTF_UNICODE),
                    (1, unit, unicode_input.KEYEVENTF_UNICODE | unicode_input.KEYEVENTF_KEYUP),
                ]
            )
        self.assertEqual(captured, expected)

    def test_reports_incomplete_input(self):
        with patch.object(unicode_input, "_send_input", return_value=0):
            with self.assertRaises(OSError):
                unicode_input.send_unicode_char("。")

    def test_worker_preserves_ascii_and_control_keys(self):
        app = main.AutoTypeApp.__new__(main.AutoTypeApp)
        app.pause_event = threading.Event()
        app.stop_event = MagicMock()
        app.stop_event.is_set.return_value = False
        app.stop_event.wait.return_value = False
        app.worker_events = queue.Queue()

        with patch.object(main, "pyautogui") as gui, patch.object(
            main, "send_unicode_char"
        ) as send_unicode:
            gui.FAILSAFE = True
            gui.FAILSAFE_POINTS = [(0, 0)]
            gui.position.return_value = (100, 100)
            app._type_worker("A中，😀\t\n", 50, ("enter",))

        gui.write.assert_called_once_with("A")
        gui.press.assert_has_calls([call("tab"), call("enter")])
        self.assertEqual(gui.press.call_count, 2)
        send_unicode.assert_has_calls([call("中"), call("，"), call("😀")])
        self.assertEqual(send_unicode.call_count, 3)
        self.assertEqual(app.worker_events.get_nowait(), ("progress", (6, 6)))
        self.assertEqual(app.worker_events.get_nowait(), ("done", None))

    def test_validation_accepts_unicode_and_rejects_controls(self):
        app = main.AutoTypeApp.__new__(main.AutoTypeApp)
        app.text_input = MagicMock()
        app.interval_var = MagicMock()
        app.interval_var.get.return_value = "150"
        app.newline_var = MagicMock()
        app.newline_var.get.return_value = "ctrl+enter"
        app._show_validation_error = MagicMock()
        app.text_input.get.return_value = "SELECT '中文，😀';\n"
        self.assertEqual(
            app._prepare_input(), ("SELECT '中文，😀';\n", 150, ("ctrl", "enter"))
        )

        app.text_input.get.return_value = "SELECT\x00"
        self.assertIsNone(app._prepare_input())
        app._show_validation_error.assert_called_once()


if __name__ == "__main__":
    unittest.main()

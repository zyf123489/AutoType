import queue
import threading
import unittest
from unittest.mock import MagicMock, call, patch

import main


class NewlineHotkeyTests(unittest.TestCase):
    def test_accepts_modifier_combinations_and_rejects_unrelated_keys(self):
        self.assertEqual(main.parse_newline_hotkey(" enter "), ("enter",))
        self.assertEqual(main.parse_newline_hotkey("Shift+Ctrl+Enter"), ("ctrl", "shift", "enter"))
        for invalid in ("", "ctrl", "ctrl++enter", "enter+enter", "win+enter", "ctrl+a"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                main.parse_newline_hotkey(invalid)

    def test_all_line_endings_use_selected_combination(self):
        app = main.AutoTypeApp.__new__(main.AutoTypeApp)
        app.pause_event = threading.Event()
        app.stop_event = MagicMock()
        app.stop_event.is_set.return_value = False
        app.stop_event.wait.return_value = False
        app.worker_events = queue.Queue()

        with patch.object(main, "pyautogui") as gui:
            app._type_worker("one\r\ntwo\nthree\rfour", 50, ("ctrl", "enter"))

        self.assertEqual(gui.hotkey.call_args_list, [call("ctrl", "enter")] * 3)
        gui.press.assert_not_called()
        self.assertEqual(app.worker_events.get_nowait(), ("progress", (10, 19)))
        self.assertEqual(app.worker_events.get_nowait(), ("progress", (19, 19)))
        self.assertEqual(app.worker_events.get_nowait(), ("done", None))


if __name__ == "__main__":
    unittest.main()

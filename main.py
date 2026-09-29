"""Type text into the focused window without using the clipboard."""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import unicodedata
from tkinter import messagebox, ttk
from typing import Any

from unicode_input import send_unicode_char

try:
    import keyboard
    import pyautogui
except ImportError as exc:  # Reported with a GUI dialog from main().
    keyboard = None  # type: ignore[assignment]
    pyautogui = None  # type: ignore[assignment]
    IMPORT_ERROR: ImportError | None = exc
else:
    IMPORT_ERROR = None


HOT_KEY = "ctrl+alt+s"
PAUSE_HOTKEY = "ctrl+alt+p"
EMERGENCY_STOP_HOTKEY = "ctrl+alt+q"
DEFAULT_INTERVAL_MS = 150
COUNTDOWN_SEC = 3
MIN_INTERVAL_MS = 50
MAX_INTERVAL_MS = 500
NEWLINE_HOTKEY_CHOICES = ("enter", "ctrl+enter", "shift+enter", "alt+enter")


def parse_newline_hotkey(value: str) -> tuple[str, ...]:
    keys = [key.strip().lower() for key in value.split("+")]
    if (
        not keys
        or any(key not in {"ctrl", "alt", "shift", "enter"} for key in keys)
        or keys.count("enter") != 1
        or len(keys) != len(set(keys))
    ):
        raise ValueError("换行键必须是 Enter，或 Ctrl / Alt / Shift 与 Enter 的组合。")
    return tuple(key for key in ("ctrl", "alt", "shift", "enter") if key in keys)


class HotkeyManager(threading.Thread):
    """Own all keyboard-library registration work on one background thread."""

    def __init__(
        self,
        event_queue: queue.Queue[str],
        result_queue: queue.Queue[tuple[bool, str, str]],
        stop_event: threading.Event,
        pause_event: threading.Event,
        typing_active_event: threading.Event,
    ) -> None:
        super().__init__(name="global-hotkey-listener", daemon=True)
        self.event_queue = event_queue
        self.result_queue = result_queue
        self.commands: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.typing_active_event = typing_active_event
        self._event_lock = threading.Lock()
        self._last_trigger_at = 0.0
        self._last_pause_at = 0.0
        self._current_hotkey = ""
        self._hook: Any = None
        self._safety_hooks: list[Any] = []

    def run(self) -> None:
        self._register_safety_hotkeys()
        self._register(HOT_KEY, initial=True)
        while True:
            command, value = self.commands.get()
            if command == "stop":
                self._remove_hook()
                return
            if command == "set" and value is not None:
                self._register(value, initial=False)

    def set_hotkey(self, hotkey: str) -> None:
        self.commands.put(("set", hotkey))

    def stop(self) -> None:
        self.commands.put(("stop", None))

    def _register(self, hotkey: str, *, initial: bool) -> None:
        previous_hotkey = self._current_hotkey
        previous_hook = self._hook
        try:
            new_hook = keyboard.add_hotkey(
                hotkey,
                self._on_trigger,
                suppress=False,
                trigger_on_release=False,
            )
        except Exception as exc:
            action = "初始化" if initial else "更新"
            self.result_queue.put(
                (False, previous_hotkey or HOT_KEY, f"全局热键{action}失败：{exc}")
            )
            return

        self._hook = new_hook
        self._current_hotkey = hotkey
        if previous_hook is not None:
            try:
                keyboard.remove_hotkey(previous_hook)
            except Exception:
                pass
        self.result_queue.put((True, hotkey, ""))

    def _register_safety_hotkeys(self) -> None:
        registered_hooks: list[Any] = []
        try:
            pause_hook = keyboard.add_hotkey(
                PAUSE_HOTKEY,
                self._on_pause,
                suppress=False,
                trigger_on_release=False,
            )
            registered_hooks.append(pause_hook)
            stop_hook = keyboard.add_hotkey(
                EMERGENCY_STOP_HOTKEY,
                self._on_emergency_stop,
                suppress=False,
                trigger_on_release=False,
            )
            registered_hooks.append(stop_hook)
        except Exception as exc:
            for hook in registered_hooks:
                try:
                    keyboard.remove_hotkey(hook)
                except Exception:
                    pass
            self.result_queue.put(
                (False, self._current_hotkey or HOT_KEY, f"安全热键初始化失败：{exc}")
            )
            return
        self._safety_hooks.extend(registered_hooks)

    def _on_trigger(self) -> None:
        now = time.monotonic()
        with self._event_lock:
            if now - self._last_trigger_at < 0.25:
                return
            self._last_trigger_at = now
        if self.typing_active_event.is_set():
            self.stop_event.set()
            self.event_queue.put("stop_requested")
            return
        self.event_queue.put("trigger")

    def _on_pause(self) -> None:
        if not self.typing_active_event.is_set():
            return
        now = time.monotonic()
        with self._event_lock:
            if now - self._last_pause_at < 0.25:
                return
            self._last_pause_at = now
            if self.pause_event.is_set():
                self.pause_event.clear()
            else:
                self.pause_event.set()
        self.event_queue.put("pause_changed")

    def _on_emergency_stop(self) -> None:
        self.stop_event.set()
        self.pause_event.clear()
        self.event_queue.put("stop_requested")

    def _remove_hook(self) -> None:
        if self._hook is None:
            return
        try:
            keyboard.remove_hotkey(self._hook)
        except Exception:
            pass
        self._hook = None
        for hook in self._safety_hooks:
            try:
                keyboard.remove_hotkey(hook)
            except Exception:
                pass
        self._safety_hooks.clear()


class AutoTypeApp:
    POLL_MS = 50

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.state = "ready"
        self.is_running = False
        self.is_closing = False
        self.countdown_after_id: str | None = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.typing_active_event = threading.Event()

        self.hotkey_events: queue.Queue[str] = queue.Queue()
        self.hotkey_results: queue.Queue[tuple[bool, str, str]] = queue.Queue()
        self.worker_events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.hotkey_manager = HotkeyManager(
            self.hotkey_events,
            self.hotkey_results,
            self.stop_event,
            self.pause_event,
            self.typing_active_event,
        )

        self.status_var = tk.StringVar(value="正在初始化全局热键...")
        self.interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL_MS))
        self.newline_var = tk.StringVar(value="enter")
        self.hotkey_var = tk.StringVar(value=HOT_KEY)
        self.active_hotkey_var = tk.StringVar(value=HOT_KEY)

        self._configure_window()
        self._build_ui()
        self.hotkey_manager.start()
        self.root.after(self.POLL_MS, self._poll_queues)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_window(self) -> None:
        self.root.title("逐字输入")
        self.root.geometry("860x730")
        self.root.minsize(700, 570)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))

        style = ttk.Style(self.root)
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Warning.TLabel", foreground="#8a3b12")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        ttk.Label(
            outer,
            text="输入内容",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).grid(row=0, column=0, sticky=tk.W, pady=(0, 6))

        editor = ttk.Frame(outer)
        editor.grid(row=1, column=0, sticky=tk.NSEW)
        editor.columnconfigure(0, weight=1)
        editor.rowconfigure(0, weight=1)

        self.text_input = tk.Text(
            editor,
            wrap=tk.NONE,
            undo=True,
            font=("Consolas", 11),
            padx=8,
            pady=8,
        )
        y_scroll = ttk.Scrollbar(editor, orient=tk.VERTICAL, command=self.text_input.yview)
        x_scroll = ttk.Scrollbar(editor, orient=tk.HORIZONTAL, command=self.text_input.xview)
        self.text_input.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.text_input.grid(row=0, column=0, sticky=tk.NSEW)
        y_scroll.grid(row=0, column=1, sticky=tk.NS)
        x_scroll.grid(row=1, column=0, sticky=tk.EW)

        settings = ttk.LabelFrame(outer, text="输入设置", padding=(10, 8))
        settings.grid(row=2, column=0, sticky=tk.EW, pady=(12, 0))
        settings.columnconfigure(5, weight=1)

        ttk.Label(settings, text="字符间隔：").grid(row=0, column=0, sticky=tk.W)
        self.interval_spinbox = ttk.Spinbox(
            settings,
            from_=MIN_INTERVAL_MS,
            to=MAX_INTERVAL_MS,
            increment=10,
            width=7,
            textvariable=self.interval_var,
        )
        self.interval_spinbox.grid(row=0, column=1, sticky=tk.W)
        ttk.Label(settings, text="ms（50-500）").grid(
            row=0, column=2, sticky=tk.W, padx=(4, 18)
        )
        ttk.Label(settings, text="当前热键：").grid(row=0, column=3, sticky=tk.W)
        ttk.Label(settings, textvariable=self.active_hotkey_var).grid(
            row=0, column=4, sticky=tk.W
        )

        ttk.Label(settings, text="换行键：").grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        self.newline_combo = ttk.Combobox(
            settings,
            textvariable=self.newline_var,
            values=NEWLINE_HOTKEY_CHOICES,
            width=18,
        )
        self.newline_combo.grid(row=1, column=1, columnspan=2, sticky=tk.W, pady=(8, 0))
        ttk.Label(settings, text="修改热键：").grid(
            row=1, column=3, sticky=tk.W, pady=(8, 0)
        )
        self.hotkey_entry = ttk.Entry(settings, textvariable=self.hotkey_var, width=18)
        self.hotkey_entry.grid(row=1, column=4, sticky=tk.W, pady=(8, 0))
        self.apply_hotkey_button = ttk.Button(
            settings, text="应用热键", command=self._apply_hotkey
        )
        self.apply_hotkey_button.grid(row=1, column=5, sticky=tk.W, pady=(8, 0))
        ttk.Label(
            settings,
            text=(
                f"暂停/继续：{PAUSE_HOTKEY}    "
                f"紧急停止：{EMERGENCY_STOP_HOTKEY}    "
                "输入中再次按启动热键也会停止"
            ),
            foreground="#a11d1d",
        ).grid(row=2, column=0, columnspan=6, sticky=tk.W, pady=(8, 0))

        actions = ttk.Frame(outer)
        actions.grid(row=3, column=0, sticky=tk.EW, pady=(12, 0))
        self.clear_button = ttk.Button(actions, text="清空文本", command=self._clear_text)
        self.clear_button.pack(side=tk.LEFT)
        self.test_button = ttk.Button(
            actions, text="开始输入", command=self._handle_trigger
        )
        self.test_button.pack(side=tk.LEFT, padx=(8, 0))
        self.pause_button = ttk.Button(
            actions, text="暂停输入", command=self._toggle_pause, state=tk.DISABLED
        )
        self.pause_button.pack(side=tk.LEFT, padx=(8, 0))
        self.stop_button = ttk.Button(
            actions, text="紧急停止", command=self._request_stop, state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(actions, textvariable=self.status_var, style="Status.TLabel").pack(
            side=tk.RIGHT
        )

        warning = (
            "开始前请确认目标输入框；英文和半角符号请使用英文输入法。\n"
            f"暂停/继续：{PAUSE_HOTKEY}；紧急停止：{EMERGENCY_STOP_HOTKEY}；鼠标移到左上角也会停止。"
        )
        ttk.Label(
            outer,
            text=warning,
            style="Warning.TLabel",
            justify=tk.LEFT,
            wraplength=810,
        ).grid(row=4, column=0, sticky=tk.EW, pady=(12, 0))

        self.text_input.focus_set()

    def _poll_queues(self) -> None:
        if self.is_closing:
            return

        while True:
            try:
                event = self.hotkey_events.get_nowait()
            except queue.Empty:
                break
            if event == "trigger":
                self._handle_trigger()
            elif event == "pause_changed":
                self._sync_pause_state()
            elif event == "stop_requested":
                self._request_stop()

        while True:
            try:
                success, hotkey, error = self.hotkey_results.get_nowait()
            except queue.Empty:
                break
            self.apply_hotkey_button.configure(state=tk.NORMAL)
            if success:
                self.hotkey_var.set(hotkey)
                self.active_hotkey_var.set(hotkey)
                if self.state == "ready":
                    self.status_var.set("就绪")
            else:
                self.hotkey_var.set(hotkey)
                self.status_var.set("热键设置失败")
                messagebox.showerror("全局热键错误", error, parent=self.root)

        while True:
            try:
                event, payload = self.worker_events.get_nowait()
            except queue.Empty:
                break
            if event == "progress" and self.state == "typing":
                current, total = payload
                self.status_var.set(f"输入中... {current}/{total}")
            elif event == "done":
                self._finish_typing("输入完成")
            elif event == "stopped":
                self._finish_typing("已紧急停止")
            elif event == "error":
                self._finish_typing("输入失败")
                messagebox.showerror("输入错误", str(payload), parent=self.root)

        self.root.after(self.POLL_MS, self._poll_queues)

    def _handle_trigger(self) -> None:
        if self.state == "countdown":
            self._cancel_countdown()
            return
        if self.state == "typing" or self.is_running:
            self._request_stop()
            return

        prepared = self._prepare_input()
        if prepared is None:
            return
        text_content, interval_ms, newline_keys = prepared
        self._start_countdown(text_content, interval_ms, newline_keys)

    def _prepare_input(self) -> tuple[str, int, tuple[str, ...]] | None:
        text_content = self.text_input.get("1.0", "end-1c")
        if not text_content:
            self.status_var.set("请输入内容")
            self.root.bell()
            return None

        try:
            interval_ms = int(self.interval_var.get().strip())
        except ValueError:
            self._show_validation_error("字符间隔必须是整数。")
            return None
        if not MIN_INTERVAL_MS <= interval_ms <= MAX_INTERVAL_MS:
            self._show_validation_error(
                f"字符间隔必须在 {MIN_INTERVAL_MS}-{MAX_INTERVAL_MS} ms 之间。"
            )
            return None

        try:
            newline_keys = parse_newline_hotkey(self.newline_var.get())
        except ValueError as exc:
            self._show_validation_error(str(exc))
            return None

        for index, char in enumerate(text_content):
            if char not in "\n\r\t" and unicodedata.category(char) in {"Cc", "Cs"}:
                line = text_content.count("\n", 0, index) + 1
                last_newline = text_content.rfind("\n", 0, index)
                column = index - last_newline
                self._show_validation_error(
                    f"第 {line} 行、第 {column} 列包含不支持的控制字符：{char!r}"
                )
                return None
        return text_content, interval_ms, newline_keys

    def _show_validation_error(self, message: str) -> None:
        self.status_var.set("配置或文本无效")
        messagebox.showwarning("无法开始输入", message, parent=self.root)

    def _start_countdown(
        self, text_content: str, interval_ms: int, newline_keys: tuple[str, ...]
    ) -> None:
        self.state = "countdown"
        self._set_controls_enabled(False)
        self.test_button.configure(state=tk.NORMAL, text="取消倒计时")
        self.stop_button.configure(state=tk.NORMAL)
        self._countdown_tick(COUNTDOWN_SEC, text_content, interval_ms, newline_keys)

    def _countdown_tick(
        self, remaining: int, text_content: str, interval_ms: int, newline_keys: tuple[str, ...]
    ) -> None:
        if self.state != "countdown":
            return
        if remaining > 0:
            self.status_var.set(f"倒计时 {remaining} 秒，请切换到目标输入框")
            self.countdown_after_id = self.root.after(
                1000,
                self._countdown_tick,
                remaining - 1,
                text_content,
                interval_ms,
                newline_keys,
            )
            return
        self.countdown_after_id = None
        self._start_typing(text_content, interval_ms, newline_keys)

    def _cancel_countdown(self) -> None:
        if self.countdown_after_id is not None:
            self.root.after_cancel(self.countdown_after_id)
            self.countdown_after_id = None
        self.state = "ready"
        self.status_var.set("已取消，等待再次触发")
        self._set_controls_enabled(True)

    def _start_typing(
        self, text_content: str, interval_ms: int, newline_keys: tuple[str, ...]
    ) -> None:
        self.state = "typing"
        self.is_running = True
        self.stop_event.clear()
        self.pause_event.clear()
        self.typing_active_event.set()
        self.test_button.configure(state=tk.NORMAL, text="停止输入")
        self.pause_button.configure(state=tk.NORMAL, text="暂停输入")
        self.stop_button.configure(state=tk.NORMAL)
        self.status_var.set(f"输入中... 0/{len(text_content)}")
        threading.Thread(
            target=self._type_worker,
            args=(text_content, interval_ms, newline_keys),
            name="text-typing-worker",
            daemon=True,
        ).start()

    def _type_worker(
        self, text_content: str, interval_ms: int, newline_keys: tuple[str, ...]
    ) -> None:
        interval_sec = interval_ms / 1000.0
        total = len(text_content)
        try:
            pyautogui.FAILSAFE = True
            pyautogui.PAUSE = 0
            for index, char in enumerate(text_content, start=1):
                while self.pause_event.is_set():
                    if self.stop_event.wait(0.05):
                        self.worker_events.put(("stopped", None))
                        return
                if self.stop_event.is_set():
                    self.worker_events.put(("stopped", None))
                    return

                if char == "\n":
                    self._press_newline(newline_keys)
                elif char == "\r":
                    # Ignore CR in CRLF, otherwise preserve it as Enter.
                    if index == total or text_content[index] != "\n":
                        self._press_newline(newline_keys)
                elif char == "\t":
                    pyautogui.press("tab")
                elif ord(char) > 127:
                    if pyautogui.FAILSAFE and pyautogui.position() in pyautogui.FAILSAFE_POINTS:
                        raise pyautogui.FailSafeException("Mouse moved to a fail-safe corner")
                    send_unicode_char(char)
                else:
                    pyautogui.write(char)

                if index == total or index % 10 == 0:
                    self.worker_events.put(("progress", (index, total)))
                if self.stop_event.wait(interval_sec):
                    self.worker_events.put(("stopped", None))
                    return
        except pyautogui.FailSafeException:
            self.worker_events.put(("stopped", None))
            return
        except Exception as exc:
            self.worker_events.put(("error", exc))
            return
        self.worker_events.put(("done", None))

    @staticmethod
    def _press_newline(newline_keys: tuple[str, ...]) -> None:
        if newline_keys == ("enter",):
            pyautogui.press("enter")
        else:
            pyautogui.hotkey(*newline_keys)

    def _toggle_pause(self) -> None:
        if self.state != "typing":
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
        else:
            self.pause_event.set()
        self._sync_pause_state()

    def _sync_pause_state(self) -> None:
        if self.state != "typing":
            return
        if self.pause_event.is_set():
            self.status_var.set(
                f"已暂停；按 {PAUSE_HOTKEY} 继续，按 {EMERGENCY_STOP_HOTKEY} 停止"
            )
            self.pause_button.configure(text="继续输入")
        else:
            self.status_var.set("继续输入中...")
            self.pause_button.configure(text="暂停输入")

    def _request_stop(self) -> None:
        if self.state == "countdown":
            self._cancel_countdown()
            self.status_var.set("已停止倒计时")
            return
        if self.state not in {"typing", "stopping"} and not self.is_running:
            return
        self.stop_event.set()
        self.pause_event.clear()
        self.state = "stopping"
        self.status_var.set("正在紧急停止...")
        self.test_button.configure(state=tk.DISABLED)
        self.pause_button.configure(state=tk.DISABLED)
        self.stop_button.configure(state=tk.DISABLED)

    def _finish_typing(self, status: str) -> None:
        self.typing_active_event.clear()
        self.pause_event.clear()
        self.stop_event.clear()
        self.is_running = False
        self.state = "ready"
        self.status_var.set(status)
        self._set_controls_enabled(True)

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        self.text_input.configure(state=state)
        self.interval_spinbox.configure(state=state)
        self.newline_combo.configure(state=state)
        self.hotkey_entry.configure(state=state)
        self.apply_hotkey_button.configure(state=state)
        self.clear_button.configure(state=state)
        self.test_button.configure(
            state=state, text="开始输入"
        )
        self.pause_button.configure(state=tk.DISABLED, text="暂停输入")
        self.stop_button.configure(state=tk.DISABLED)

    def _apply_hotkey(self) -> None:
        if self.state != "ready":
            return
        hotkey = self.hotkey_var.get().strip().lower()
        if not hotkey:
            self._show_validation_error("全局热键不能为空。")
            return
        if hotkey.replace(" ", "") in {PAUSE_HOTKEY, EMERGENCY_STOP_HOTKEY}:
            self._show_validation_error("启动热键不能与暂停或紧急停止热键相同。")
            return
        self.apply_hotkey_button.configure(state=tk.DISABLED)
        self.status_var.set("正在更新全局热键...")
        self.hotkey_manager.set_hotkey(hotkey)

    def _clear_text(self) -> None:
        if self.state != "ready":
            return
        self.text_input.delete("1.0", tk.END)
        self.status_var.set("文本已清空")
        self.text_input.focus_set()

    def _on_close(self) -> None:
        if self.is_closing:
            return
        self.is_closing = True
        self.stop_event.set()
        self.pause_event.clear()
        self.typing_active_event.clear()
        if self.countdown_after_id is not None:
            self.root.after_cancel(self.countdown_after_id)
            self.countdown_after_id = None
        self.hotkey_manager.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    if IMPORT_ERROR is not None:
        root.withdraw()
        messagebox.showerror(
            "缺少依赖",
            "程序缺少运行依赖。请先执行：\n\n"
            "pip install -r requirements.txt\n\n"
            f"详细信息：{IMPORT_ERROR}",
            parent=root,
        )
        root.destroy()
        return

    AutoTypeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone
from typing import Callable


@dataclass
class SwitchWindow:
    start: dt_time
    end: dt_time


class StrategySwitcher:
    def __init__(
        self,
        *,
        log: Callable[[str], None],
        loop_v1,
        loop_v2,
        window: SwitchWindow,
    ):
        self.log = log
        self.loop_v1 = loop_v1
        self.loop_v2 = loop_v2
        self.window = window

        self._active = None  # "v1" | "v2"
        self._signals_on = False
        self._live_feed_on = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._active = "v2" if self._in_window() else "v1"

    def _in_window(self) -> bool:
        now_t = datetime.now(timezone.utc).time()
        start = self.window.start
        end = self.window.end
        if start == end:
            return True
        if start <= end:
            return start <= now_t < end
        return now_t >= start or now_t < end

    def _current_loop(self):
        return self.loop_v2 if self._active == "v2" else self.loop_v1

    def _other_loop(self):
        return self.loop_v1 if self._active == "v2" else self.loop_v2

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _switch_to(self, target: str):
        if target == self._active:
            return
        current = self._current_loop()
        next_loop = self.loop_v2 if target == "v2" else self.loop_v1

        self.log(f"Switching strategy: {self._active} -> {target} (flatten on boundary).")
        current.flatten_active("switch_window")
        if self._signals_on:
            current.stop_signals()
        if self._live_feed_on:
            current.stop_live_feed()

        self._active = target
        if self._signals_on:
            next_loop.start_signals()
        if self._live_feed_on:
            next_loop.start_live_feed()

    def _run(self):
        while not self._stop.is_set():
            try:
                should_be = "v2" if self._in_window() else "v1"
                if should_be != self._active:
                    self._switch_to(should_be)
            except Exception as e:
                self.log(f"Strategy switcher error: {e}")
            time.sleep(1.0)

    # ---------- public API ----------
    def start_signals(self):
        self._signals_on = True
        self._current_loop().start_signals()

    def stop_signals(self):
        self._signals_on = False
        self._current_loop().stop_signals()

    def start_live_feed(self):
        self._live_feed_on = True
        self._current_loop().start_live_feed()

    def stop_live_feed(self):
        self._live_feed_on = False
        self._current_loop().stop_live_feed()

    def start_live_watchdog(self):
        self.loop_v1.start_live_watchdog()
        self.loop_v2.start_live_watchdog()

    def flatten_active(self, reason: str = "external_lock"):
        self._current_loop().flatten_active(reason)

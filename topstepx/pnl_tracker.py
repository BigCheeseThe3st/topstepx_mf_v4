from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from .user_hub import UserHubClient, UserTrade, UserPosition


@dataclass
class DailyState:
    date: str
    realized_ticks: float
    trade_ids: set[int]


class PnLTracker:
    def __init__(
        self,
        *,
        token_factory: Callable[[], str],
        log: Callable[[str], None],
        get_account_id: Callable[[], int],
        get_tick_value: Callable[[], float],
        get_tick_size: Callable[[], float],
        get_last_price: Callable[[], Optional[float]],
        update_daily_pnl: Callable[[float, float], None],
        daily_profit_ticks: float,
        daily_reset_hour: int,
        daily_reset_minute: int,
        storage_path: str,
        poll_interval_sec: int = 300,
    ):
        self.token_factory = token_factory
        self.log = log
        self.get_account_id = get_account_id
        self.get_tick_value = get_tick_value
        self.get_tick_size = get_tick_size
        self.get_last_price = get_last_price
        self.update_daily_pnl = update_daily_pnl
        self.daily_profit_ticks = float(daily_profit_ticks)
        self.daily_reset_hour = int(daily_reset_hour)
        self.daily_reset_minute = int(daily_reset_minute)
        self.storage_path = storage_path
        self.poll_interval_sec = int(poll_interval_sec)

        self._hub = UserHubClient(token_factory=token_factory, log=log)
        self._hub.set_on_trade(self._on_trade)
        self._hub.set_on_position(self._on_position)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_update_ts: float = 0.0
        self._last_lock_state: bool = False
        self._on_lock: Optional[Callable[[str], None]] = None

        self._daily_state = DailyState(date=self._session_date_local().isoformat(), realized_ticks=0.0, trade_ids=set())
        self._positions = {}  # contract_id -> (ptype, size, avg)

        self._load_state()

    def set_on_lock(self, cb: Callable[[str], None]):
        self._on_lock = cb

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self._hub.stop()
        except Exception:
            pass

    def is_locked(self) -> bool:
        return self._daily_total_ticks() >= self.daily_profit_ticks

    def _session_date_local(self, dt: datetime | None = None):
        now = dt or datetime.now().astimezone()
        reset_time = now.replace(
            hour=self.daily_reset_hour,
            minute=self.daily_reset_minute,
            second=0,
            microsecond=0,
        ).time()
        if now.time() < reset_time:
            return (now.date() - timedelta(days=1))
        return now.date()

    def _reset_if_needed(self, dt: datetime | None = None):
        session_date = self._session_date_local(dt)
        if self._daily_state.date != session_date.isoformat():
            self._daily_state = DailyState(date=session_date.isoformat(), realized_ticks=0.0, trade_ids=set())
            self._positions = {}
            self._save_state()

    def _daily_total_ticks(self) -> float:
        return float(self._daily_state.realized_ticks) + float(self._unrealized_ticks())

    def _unrealized_ticks(self) -> float:
        price = self.get_last_price()
        if price is None:
            return 0.0
        tick_size = float(self.get_tick_size() or 0.0)
        if tick_size <= 0:
            return 0.0
        total = 0.0
        for _, (ptype, size, avg) in self._positions.items():
            if not size or avg is None:
                continue
            if ptype == 1:  # long
                pnl_price = price - avg
            else:  # short
                pnl_price = avg - price
            total += (pnl_price / tick_size) * abs(int(size))
        return total

    def _on_trade(self, t: UserTrade):
        try:
            if t.voided:
                return
            if t.trade_id is None:
                return
            if t.trade_id in self._daily_state.trade_ids:
                return
            try:
                account_id = self.get_account_id()
                if t.account_id is not None and int(t.account_id) != int(account_id):
                    return
            except Exception:
                return
            if t.creation_timestamp:
                trade_dt = datetime.fromisoformat(t.creation_timestamp.replace("Z", "+00:00")).astimezone()
            else:
                trade_dt = datetime.now().astimezone()
            self._reset_if_needed(trade_dt)
            if t.profit_and_loss is None:
                return
            tick_value = float(self.get_tick_value() or 0.0)
            if tick_value <= 0:
                return
            pnl_ticks = float(t.profit_and_loss) / tick_value
            self._daily_state.trade_ids.add(int(t.trade_id))
            self._daily_state.realized_ticks += pnl_ticks
            self._save_state()
            self._maybe_update()
        except Exception as e:
            self.log(f"PnL tracker trade error: {e}")

    def _on_position(self, p: UserPosition):
        try:
            try:
                account_id = self.get_account_id()
                if p.account_id is not None and int(p.account_id) != int(account_id):
                    return
            except Exception:
                return
            if not p.contract_id:
                return
            self._positions[str(p.contract_id)] = (p.type, p.size, p.average_price)
            self._maybe_update()
        except Exception as e:
            self.log(f"PnL tracker position error: {e}")

    def _maybe_update(self):
        now_ts = time.time()
        if now_ts - self._last_update_ts < 0.5:
            return
        self._last_update_ts = now_ts
        total = self._daily_total_ticks()
        unreal = self._unrealized_ticks()
        realized = float(self._daily_state.realized_ticks)
        self.update_daily_pnl(realized, unreal)
        self._check_lock(total)

    def _check_lock(self, total_ticks: float):
        locked = total_ticks >= self.daily_profit_ticks
        if locked and not self._last_lock_state:
            self._last_lock_state = True
            if self._on_lock:
                try:
                    self._on_lock("daily_profit_lock")
                except Exception:
                    pass
        if not locked and self._last_lock_state:
            self._last_lock_state = False

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.token_factory():
                    self._hub.start()
                    try:
                        account_id = self.get_account_id()
                        self._hub.subscribe_trades(account_id)
                    except Exception:
                        pass
                self._maybe_update()
            except Exception as e:
                self.log(f"PnL tracker error: {e}")
            time.sleep(self.poll_interval_sec)

    def _load_state(self):
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            date = raw.get("date")
            realized = float(raw.get("realized_ticks", 0.0))
            trade_ids = set(int(x) for x in raw.get("trade_ids", []) if x is not None)
            if date:
                self._daily_state = DailyState(date=date, realized_ticks=realized, trade_ids=trade_ids)
        except Exception:
            return

    def _save_state(self):
        try:
            data = {
                "date": self._daily_state.date,
                "realized_ticks": self._daily_state.realized_ticks,
                "trade_ids": list(self._daily_state.trade_ids),
            }
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            return

# © 2026 BigCheeseThe3st
# Licensed under NCSAL v1.1 (see LICENSE.txt)
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Any
from datetime import datetime, time as dt_time, timezone, timedelta

import pandas as pd

from .api_client import TopstepXClient, ApiError
from .indicators import bollinger_bands, rsi

@dataclass
class BandsSnapshot:
    label: str
    close: float
    lower: float
    mid: float
    upper: float

def bars_to_df(bars: list[dict]) -> pd.DataFrame:
    # Expect bars with keys: timestamp (ISO), open/high/low/close or o/h/l/c
    rows = []
    for b in bars:
        ts = b.get("timestamp") or b.get("time") or b.get("t")
        o = b.get("open", b.get("o"))
        h = b.get("high", b.get("h"))
        l = b.get("low", b.get("l"))
        c = b.get("close", b.get("c"))
        if ts is None or c is None:
            continue
        rows.append((ts, float(o) if o is not None else None,
                     float(h) if h is not None else None,
                     float(l) if l is not None else None,
                     float(c)))
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    df = df.dropna(subset=["timestamp", "close"]).sort_values("timestamp")
    return df

def get_bands_snapshot(
    client: TopstepXClient,
    contract_id: str,
    tf: str,
    *,
    live: bool,
    length: int = 10,
    mult: float = 1.5,
    limit: int = 200,
) -> Optional[BandsSnapshot]:
    data = client.retrieve_bars(contract_id, tf, limit=limit, live=live, include_partial_bar=live)
    bars = data.get("bars") or data.get("candles") or data.get("data") or []
    if not bars:
        data = client.retrieve_bars(contract_id, tf, limit=limit, live=live, include_partial_bar=not live)
        bars = data.get("bars") or data.get("candles") or data.get("data") or []
    if len(bars) < length:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=7)
        data = client.retrieve_bars(
            contract_id,
            tf,
            limit=20000,
            live=live,
            start_time=start_dt.isoformat(),
            end_time=end_dt.isoformat(),
            include_partial_bar=False,
        )
        hist = data.get("bars") or data.get("candles") or data.get("data") or []
        if hist:
            bars = hist
    df = bars_to_df(bars)
    if df.empty or len(df) < length:
        return None
    last = df.iloc[-1]
    if df["high"].notna().any() and df["low"].notna().any():
        price = (df["high"] + df["low"] + df["close"]) / 3.0
    else:
        price = df["close"]
    bb = bollinger_bands(price, length=length, mult=mult)
    label = last["timestamp"].isoformat()
    return BandsSnapshot(label=label, close=float(last["close"]), lower=bb.lower, mid=bb.mid, upper=bb.upper)

def get_rsi(
    client: TopstepXClient,
    contract_id: str,
    tf: str,
    *,
    live: bool,
    length: int = 14,
    limit: int = 200,
) -> Optional[float]:
    data = client.retrieve_bars(contract_id, tf, limit=limit, live=live, include_partial_bar=False)
    bars = data.get("bars") or data.get("candles") or data.get("data") or []
    df = bars_to_df(bars)
    if df.empty or len(df) < length + 1:
        return None
    return rsi(df["close"], length=length)

def _within_window_utc(windows_str: str) -> bool:
    try:
        windows_str = (windows_str or "").strip()
        if not windows_str:
            return True
        ranges = [r.strip() for r in windows_str.split(",") if r.strip()]
    except Exception:
        return True
    now_t = datetime.now(timezone.utc).time()
    for r in ranges:
        if "-" not in r:
            continue
        start_str, end_str = [p.strip() for p in r.split("-", 1)]
        try:
            start_h, start_m = [int(x) for x in start_str.split(":")]
            end_h, end_m = [int(x) for x in end_str.split(":")]
            start = dt_time(start_h, start_m)
            end = dt_time(end_h, end_m)
        except Exception:
            continue
        if start == end:
            return True
        if start <= end:
            if start <= now_t <= end:
                return True
        else:
            if now_t >= start or now_t <= end:
                return True
    return False

class SignalEngine:
    """
    High-frequency variant:
      - Trigger on 1m close outside 1m bands
      - Trend filter: 5m close must be on the correct side of 5m midline
    """
    def __init__(
        self,
        client: TopstepXClient,
        contract_id: str,
        live: bool,
        offset_seconds: int,
        log: Callable[[str], None],
        on_signal: Callable[[str, Dict[str, Any]], None],
        get_live_price: Callable[[], Optional[float]],
        bb_len: int = 10,
        bb_mult: float = 1.5,
        rsi_enabled: bool = False,
        rsi_len: int = 14,
        rsi_overbought: float = 70.0,
        rsi_oversold: float = 30.0,
        retrace_ticks: int = 2,
        tick_size: float = 0.1,
        trend_filter_mode: str = "5m close vs midline",
        trade_window_enabled: bool = False,
        trade_windows: str = "00:00-23:59",
    ):
        self.client = client
        self.contract_id = contract_id
        self.live = live
        self.offset = int(offset_seconds)
        self.bb_len = bb_len
        self.bb_mult = bb_mult
        self.rsi_enabled = rsi_enabled
        self.rsi_len = rsi_len
        self.rsi_overbought = rsi_overbought
        self.rsi_oversold = rsi_oversold
        self.retrace_ticks = int(retrace_ticks)
        self.tick_size = float(tick_size)
        self.trend_filter_mode = str(trend_filter_mode)
        self.trade_window_enabled = trade_window_enabled
        self.trade_windows = trade_windows
        self.log = log
        self.on_signal = on_signal
        self.get_live_price = get_live_price

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._snap_thread: Optional[threading.Thread] = None
        self._last_1m_label: Optional[str] = None
        self._crossed_long = False
        self._crossed_short = False
        self._extreme_low = None
        self._extreme_high = None
        self._last_live_log_ts = 0.0
        self._last_live_seen_ts = 0.0
        self._last_missing_live_log_ts = 0.0
        self._last_midline_price = None
        self._snap_lock = threading.Lock()
        self._snap_1m: Optional[BandsSnapshot] = None
        self._snap_5m: Optional[BandsSnapshot] = None
        self._snap_rsi: Optional[float] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._snap_thread = threading.Thread(target=self._snapshot_loop, daemon=True)
        self._snap_thread.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _sleep_to_next_1m(self):
        now = time.time()
        utc = time.gmtime(now)
        sec_since_min = utc.tm_sec
        wait = (60 - sec_since_min) + self.offset
        if wait < 0:
            wait = self.offset
        time.sleep(wait)

    def _snapshot_loop(self):
        self.log(f"SignalEngine started. contractId={self.contract_id} live={self.live} offset={self.offset}s")
        self._sleep_to_next_1m()
        while not self._stop.is_set():
            try:
                s1 = get_bands_snapshot(
                    self.client,
                    self.contract_id,
                    "1m",
                    live=self.live,
                    length=self.bb_len,
                    mult=self.bb_mult,
                    limit=400,
                )
                s5 = get_bands_snapshot(
                    self.client,
                    self.contract_id,
                    "5m",
                    live=self.live,
                    length=self.bb_len,
                    mult=self.bb_mult,
                    limit=400,
                )
                if not s1 or not s5:
                    self.log(
                        f"SignalEngine: No 1m/5m bars returned (contractId={self.contract_id} live={self.live}, bb_len={self.bb_len})."
                    )
                    self._sleep_to_next_1m()
                    continue
                rsi_val = None
                if self.rsi_enabled:
                    rsi_val = get_rsi(
                        self.client,
                        self.contract_id,
                        "1m",
                        live=self.live,
                        length=self.rsi_len,
                        limit=400,
                    )
                    if rsi_val is None:
                        self.log("1m: RSI unavailable.")
                with self._snap_lock:
                    self._snap_1m = s1
                    self._snap_5m = s5
                    self._snap_rsi = rsi_val
                self.log(
                    f"5m label={s5.label} c={s5.close:.2f} [L={s5.lower:.2f} M={s5.mid:.2f} U={s5.upper:.2f}] | "
                    f"1m label={s1.label} c={s1.close:.2f} [L={s1.lower:.2f} M={s1.mid:.2f} U={s1.upper:.2f}]"
                )
                self._sleep_to_next_1m()
            except ApiError as e:
                self.log(f"SignalEngine error: {e}")
                self._sleep_to_next_1m()
            except Exception as e:
                self.log(f"SignalEngine error: {e}")
                self._sleep_to_next_1m()

    def _run(self):
        while not self._stop.is_set():
            try:
                if self.trade_window_enabled and not _within_window_utc(self.trade_windows):
                    now_str = datetime.now(timezone.utc).strftime("%H:%M")
                    self.log(
                        f"SignalEngine: outside trade window (UTC). now={now_str} "
                        f"window={self.trade_windows}"
                    )
                    time.sleep(1.0)
                    continue

                with self._snap_lock:
                    s1 = self._snap_1m
                    s5 = self._snap_5m
                    rsi_val = self._snap_rsi
                if not s1 or not s5:
                    time.sleep(0.25)
                    continue

                if s1.label != self._last_1m_label:
                    self._last_1m_label = s1.label
                    self._crossed_long = False
                    self._crossed_short = False
                    self._extreme_low = None
                    self._extreme_high = None

                price = self.get_live_price()
                if price is None:
                    now_ts = time.time()
                    if now_ts - self._last_missing_live_log_ts >= 10.0:
                        self._last_missing_live_log_ts = now_ts
                        self.log("Live price missing (no quote yet).")
                    time.sleep(0.25)
                    continue
                self._last_live_seen_ts = time.time()
                prev_price = self._last_midline_price
                self._last_midline_price = price

                # Periodic live debug to visualize trigger context
                now_ts = time.time()
                if now_ts - self._last_live_log_ts >= 1.0:
                    self._last_live_log_ts = now_ts
                    self.log(
                        f"Live 1m bands: price={price:.2f} "
                        f"L={s1.lower:.2f} M={s1.mid:.2f} U={s1.upper:.2f}"
                    )

                # Track crosses and extremes (require live price at both 1m and 5m bands)
                if price <= s1.lower and price <= s5.lower:
                    self._crossed_long = True
                    self._extreme_low = price if self._extreme_low is None else min(self._extreme_low, price)
                if price >= s1.upper and price >= s5.upper:
                    self._crossed_short = True
                    self._extreme_high = price if self._extreme_high is None else max(self._extreme_high, price)

                triggered = False
                direction = None
                trigger_price = None
                retrace_dist = self.retrace_ticks * self.tick_size

                if self._crossed_long and self._extreme_low is not None:
                    if price >= self._extreme_low + retrace_dist:
                        direction = "LONG"
                        trigger_price = price
                        self._crossed_long = False
                        self._extreme_low = None
                        triggered = True
                if not triggered and self._crossed_short and self._extreme_high is not None:
                    if price <= self._extreme_high - retrace_dist:
                        direction = "SHORT"
                        trigger_price = price
                        self._crossed_short = False
                        self._extreme_high = None
                        triggered = True

                if not triggered:
                    time.sleep(0.25)
                    continue

                # Optional RSI filter (1m)
                if self.rsi_enabled and rsi_val is not None:
                    if direction == "LONG" and rsi_val > self.rsi_oversold:
                        self.log(f"1m: RSI filter blocked LONG (rsi={rsi_val:.1f} > {self.rsi_oversold}).")
                        continue
                    if direction == "SHORT" and rsi_val < self.rsi_overbought:
                        self.log(f"1m: RSI filter blocked SHORT (rsi={rsi_val:.1f} < {self.rsi_overbought}).")
                        continue

                # Trend filter disabled for now

                payload = {
                    "direction": direction,
                    "trigger_price": trigger_price,
                    "s1": s1.__dict__,
                    "s5": s5.__dict__,
                }
                self.log(f"*** SIGNAL {direction} ***")
                self.on_signal(direction, payload)
                time.sleep(0.05)

            except Exception as e:
                self.log(f"SignalEngine error: {e}")
                time.sleep(0.25)

# © 2026 BigCheeseThe3st
# Licensed under NCSAL v1.1 (see LICENSE.txt)
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, time as dt_time
from typing import List, Optional, Callable
import concurrent.futures as cf
import os

import pandas as pd

from .api_client import TopstepXClient
from .indicators import atr, rsi
from .signal_engine import bars_to_df

MAX_BARS = 20000
_DF15 = None
_DF5 = None
_DF1 = None

def _init_worker(df15, df5, df1):
    global _DF15, _DF5, _DF1
    _DF15 = df15
    _DF5 = df5
    _DF1 = df1

def _run_one(cfg: BacktestConfig) -> GridResult:
    res = _run_backtest_core(_DF15, _DF5, _DF1, cfg)
    return GridResult(
        cfg=cfg,
        trades=len(res.trades),
        wins=res.wins,
        losses=res.losses,
        win_rate=res.win_rate,
        total_pnl_ticks=res.total_pnl_ticks,
        total_pnl_usd=res.total_pnl_usd,
        skipped=res.skipped,
        max_consecutive_loss_usd=res.max_consecutive_loss_usd,
        data_start=res.data_start,
        data_end=res.data_end,
    )

@dataclass
class BacktestConfig:
    contract_id: str
    live: bool
    start_time: datetime
    end_time: datetime
    bb_len: int = 10
    bb_mult: float = 1.5
    atr_len: int = 14
    rsi_enabled: bool = False
    rsi_len: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    trend_filter_mode: str = "5m close vs midline"
    retrace_ticks: int = 2
    sl_mode: str = "Fixed ticks"
    tp_mid5: bool = True
    tp_mid15: bool = True
    tp_fixed: bool = True
    tp_band: bool = False
    tp_atr: bool = False
    sl_ticks: int = 20
    tp_ticks: int = 30
    sl_mult: float = 0.5
    tp_mult: float = 1.0
    tick_size: float = 0.1
    entry_offset_ticks: int = 0
    tick_value: float = 10.0
    lot_size: int = 1
    entry_timeout_min: int = 15
    entry_mode: str = "Immediate"
    cancel_on_midline_before_fill: bool = False
    trade_window_enabled: bool = False
    trade_windows: str = "00:00-23:59"
    trade_windows_v1: str = "00:00-05:30"
    trade_windows_v2: str = "08:30-16:00"
    cooldown_enabled: bool = True
    cooldown_seconds_v1: int = 55
    cooldown_seconds_v2: int = 120
    daily_profit_enabled: bool = True
    daily_profit_ticks: float = 180.0
    daily_reset_hour: int = 23
    daily_reset_minute: int = 0

@dataclass
class TradeResult:
    direction: str
    trigger_time: datetime
    trigger_close: float
    confirm_time: Optional[datetime]
    confirm_close: Optional[float]
    entry_limit: float
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str  # "TP", "SL", "EOD"
    pnl_price: float
    pnl_ticks: float
    pnl_usd: float
    sl_price: float
    tp_prices: List[float]
    tp_hit_price: Optional[float] = None

@dataclass
class SkippedSignal:
    trigger_time: datetime
    direction: str
    reason: str
    trigger_close: float
    entry_limit: Optional[float]
    cancel_levels: List[float]

@dataclass
class BacktestResult:
    trades: List[TradeResult]
    skipped_signals: List[SkippedSignal]
    wins: int
    losses: int
    win_rate: float
    total_pnl_ticks: float
    avg_pnl_ticks: float
    skipped: int
    total_pnl_price: float
    total_pnl_usd: float
    max_consecutive_loss_usd: float
    max_consecutive_loss_start: Optional[datetime]
    max_consecutive_loss_end: Optional[datetime]
    equity_at_max_consec_start: float
    equity_at_max_consec_end: float
    max_drawdown_usd: float
    max_drawdown_start: Optional[datetime]
    max_drawdown_end: Optional[datetime]
    min_equity: float
    min_equity_time: Optional[datetime]
    trailing_limit_breached: bool
    trailing_limit_min_margin: float
    trailing_limit_breach_time: Optional[datetime]
    data_start: Optional[datetime]
    data_end: Optional[datetime]

@dataclass
class GridResult:
    cfg: BacktestConfig
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl_ticks: float
    total_pnl_usd: float
    skipped: int
    max_consecutive_loss_usd: float
    data_start: Optional[datetime]
    data_end: Optional[datetime]

def _unit_delta(unit: int, unit_number: int) -> timedelta:
    if unit == 1:  # Second
        return timedelta(seconds=unit_number)
    if unit == 2:  # Minute
        return timedelta(minutes=unit_number)
    if unit == 3:  # Hour
        return timedelta(hours=unit_number)
    if unit == 4:  # Day
        return timedelta(days=unit_number)
    if unit == 5:  # Week
        return timedelta(weeks=unit_number)
    if unit == 6:  # Month (approx)
        return timedelta(days=30 * unit_number)
    raise ValueError(f"Unsupported unit {unit}")

def _within_window(ts: pd.Timestamp, windows_str: str) -> bool:
    try:
        windows_str = (windows_str or "").strip()
        if not windows_str:
            return True
        ranges = [r.strip() for r in windows_str.split(",") if r.strip()]
    except Exception:
        return True
    t = ts.time()
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
            if start <= t <= end:
                return True
        else:
            if t >= start or t <= end:
                return True
    return False


def _strategy_for_time(ts: pd.Timestamp, cfg: BacktestConfig) -> Optional[str]:
    if not cfg.trade_window_enabled:
        return "v1"
    in_v2 = _within_window(ts, cfg.trade_windows_v2)
    in_v1 = _within_window(ts, cfg.trade_windows_v1)
    if in_v2:
        return "v2"
    if in_v1:
        return "v1"
    return None

def _session_date(ts: pd.Timestamp, reset_hour: int, reset_minute: int):
    try:
        reset_t = dt_time(reset_hour, reset_minute)
    except Exception:
        reset_t = dt_time(0, 0)
    t = ts.time()
    if t < reset_t:
        return (ts.date() - timedelta(days=1))
    return ts.date()

def _fetch_bars_range(
    client: TopstepXClient,
    contract_id: str,
    *,
    unit: int,
    unit_number: int,
    start: datetime,
    end: datetime,
    live: bool,
    limit: int = MAX_BARS,
) -> pd.DataFrame:
    all_rows = []
    delta = _unit_delta(unit, unit_number)
    chunk = delta * limit
    cur = start
    while cur < end:
        chunk_end = min(cur + chunk, end)
        data = client.retrieve_bars(
            contract_id,
            timeframe=None,
            limit=limit,
            live=live,
            unit=unit,
            unit_number=unit_number,
            start_time=cur.isoformat(),
            end_time=chunk_end.isoformat(),
            include_partial_bar=False,
        )
        bars = data.get("bars") or data.get("candles") or data.get("data") or []
        df = bars_to_df(bars)
        if not df.empty:
            all_rows.append(df)
        cur = chunk_end
    if not all_rows:
        return pd.DataFrame()
    df_all = pd.concat(all_rows, ignore_index=True)
    df_all = df_all.drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    return df_all

def _add_bbands(df: pd.DataFrame, length: int, mult: float) -> pd.DataFrame:
    if "high" in df and "low" in df and df["high"].notna().any() and df["low"].notna().any():
        price = (df["high"] + df["low"] + df["close"]) / 3.0
    else:
        price = df["close"]
    roll = price.rolling(length)
    mid = roll.mean()
    std = roll.std(ddof=0)
    df["bb_mid"] = mid
    df["bb_upper"] = mid + mult * std
    df["bb_lower"] = mid - mult * std
    return df

def _add_atr(df: pd.DataFrame, length: int) -> pd.DataFrame:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            (df["high"] - df["low"]).abs(),
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.rolling(length).mean()
    return df

def _prepare_data(client: TopstepXClient, cfg: BacktestConfig):
    start = cfg.start_time.astimezone(timezone.utc)
    end = cfg.end_time.astimezone(timezone.utc)

    df15 = _fetch_bars_range(client, cfg.contract_id, unit=2, unit_number=15, start=start, end=end, live=cfg.live)
    df5 = _fetch_bars_range(client, cfg.contract_id, unit=2, unit_number=5, start=start, end=end, live=cfg.live)
    df1 = _fetch_bars_range(client, cfg.contract_id, unit=2, unit_number=1, start=start, end=end, live=cfg.live)

    if df15.empty or df5.empty or df1.empty:
        return None
    return df15, df5, df1

def _run_backtest_core(df15_raw: pd.DataFrame, df5_raw: pd.DataFrame, df1_raw: pd.DataFrame, cfg: BacktestConfig) -> BacktestResult:
    df15 = df15_raw.copy()
    df5 = df5_raw.copy()
    df1 = df1_raw.copy()

    for df in (df15, df5, df1):
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df.set_index("timestamp", inplace=True)

    df15 = _add_bbands(df15, cfg.bb_len, cfg.bb_mult)
    df5 = _add_bbands(df5, cfg.bb_len, cfg.bb_mult)
    df1 = _add_bbands(df1, cfg.bb_len, cfg.bb_mult)
    df5 = _add_atr(df5, cfg.atr_len)
    if cfg.rsi_enabled:
        df1["rsi"] = df1["close"].rolling(cfg.rsi_len + 1).apply(
            lambda s: rsi(s, length=cfg.rsi_len) if len(s) >= cfg.rsi_len + 1 else float("nan"),
            raw=False,
        )

    idx15 = df15.index.to_numpy()
    idx5 = df5.index.to_numpy()
    idx1 = df1.index.to_numpy()
    data_start = df1.index.min().to_pydatetime()
    data_end = df1.index.max().to_pydatetime()

    trades: List[TradeResult] = []
    skipped_signals: List[SkippedSignal] = []
    skipped = 0
    cooldown_until: Optional[pd.Timestamp] = None
    in_trade_until: Optional[pd.Timestamp] = None
    daily_realized_ticks = 0.0
    daily_lock = False
    current_session_date = None

    def _skip(reason: str, direction: str, t: pd.Timestamp, close: float, entry_limit: Optional[float], cancel_levels: Optional[List[float]] = None):
        nonlocal skipped
        skipped += 1
        skipped_signals.append(
            SkippedSignal(
                trigger_time=t.to_pydatetime(),
                direction=direction,
                reason=reason,
                trigger_close=close,
                entry_limit=entry_limit,
                cancel_levels=cancel_levels or [],
            )
        )

    def row_at_or_before(df: pd.DataFrame, idx, t):
        pos = idx.searchsorted(t, side="right") - 1
        if pos < 0:
            return None
        return df.iloc[pos]

    v1_crossed_long = False
    v1_crossed_short = False
    v1_extreme_low = None
    v1_extreme_high = None
    v2_crossed_long = False
    v2_crossed_short = False

    for i, t in enumerate(idx1):
        session_date = _session_date(pd.Timestamp(t), cfg.daily_reset_hour, cfg.daily_reset_minute)
        if current_session_date != session_date:
            current_session_date = session_date
            daily_realized_ticks = 0.0
            daily_lock = False
        if in_trade_until is not None and t < in_trade_until:
            continue
        row1 = df1.iloc[i]
        if pd.isna(row1.get("bb_mid")):
            continue
        if cooldown_until is not None and t < cooldown_until:
            continue
        close1 = float(row1["close"])
        lower1 = float(row1["bb_lower"])
        upper1 = float(row1["bb_upper"])
        high1 = float(row1["high"]) if not pd.isna(row1.get("high")) else close1
        low1 = float(row1["low"]) if not pd.isna(row1.get("low")) else close1

        row5 = row_at_or_before(df5, idx5, t)
        row15 = row_at_or_before(df15, idx15, t)
        if row5 is None or row15 is None:
            continue
        if pd.isna(row5.get("bb_mid")) or pd.isna(row15.get("bb_mid")):
            continue

        strategy = _strategy_for_time(pd.Timestamp(t), cfg)
        if strategy is None:
            v1_crossed_long = False
            v1_crossed_short = False
            v1_extreme_low = None
            v1_extreme_high = None
            v2_crossed_long = False
            v2_crossed_short = False
            continue

        direction = None
        trigger_price = None

        if strategy == "v1":
            # Track cross outside bands and retrace by N ticks (require 1m price at both 1m and 5m bands)
            if low1 <= lower1 and low1 <= float(row5["bb_lower"]):
                v1_crossed_long = True
                v1_extreme_low = low1 if v1_extreme_low is None else min(v1_extreme_low, low1)
            if high1 >= upper1 and high1 >= float(row5["bb_upper"]):
                v1_crossed_short = True
                v1_extreme_high = high1 if v1_extreme_high is None else max(v1_extreme_high, high1)

            retrace_dist = cfg.retrace_ticks * cfg.tick_size
            if v1_crossed_long and v1_extreme_low is not None and high1 >= v1_extreme_low + retrace_dist:
                direction = "LONG"
                trigger_price = v1_extreme_low + retrace_dist
                v1_crossed_long = False
                v1_extreme_low = None
            elif v1_crossed_short and v1_extreme_high is not None and low1 <= v1_extreme_high - retrace_dist:
                direction = "SHORT"
                trigger_price = v1_extreme_high - retrace_dist
                v1_crossed_short = False
                v1_extreme_high = None
        else:
            # v2 edge trigger on band touch
            is_below = low1 <= lower1 and low1 <= float(row5["bb_lower"])
            is_above = high1 >= upper1 and high1 >= float(row5["bb_upper"])

            if is_below and not v2_crossed_long:
                direction = "LONG"
                trigger_price = close1
            if not is_below:
                v2_crossed_long = False

            if direction is None and is_above and not v2_crossed_short:
                direction = "SHORT"
                trigger_price = close1
            if not is_above:
                v2_crossed_short = False

            if direction == "LONG":
                v2_crossed_long = True
            elif direction == "SHORT":
                v2_crossed_short = True

        if not direction:
            continue
        # Strategy window already enforced by _strategy_for_time
        if cfg.daily_profit_enabled and daily_lock:
            _skip("daily_profit_lock", direction, t, close1, None, [])
            continue

        if cfg.rsi_enabled:
            rsi_val = float(row1.get("rsi")) if "rsi" in row1 else float("nan")
            if pd.isna(rsi_val):
                _skip("rsi_unavailable", "LONG", t, close1, None, [])
                continue
            if direction == "LONG" and rsi_val > cfg.rsi_oversold:
                _skip("rsi_filter", direction, t, close1, None, [])
                continue
            if direction == "SHORT" and rsi_val < cfg.rsi_overbought:
                _skip("rsi_filter", direction, t, close1, None, [])
                continue

        # Trend filter disabled for now

        confirm_time = t.to_pydatetime()
        confirm_close = close1
        entry_limit = trigger_price if trigger_price is not None else close1
        entry_time = t
        entry_price = entry_limit
        cancel_levels: List[float] = []
        trade_strategy = strategy

        sl_price = None
        tp_price = None

        def _distance_from_mode(mode: str, ticks: int, mult: float) -> Optional[float]:
            if mode == "Fixed ticks":
                return ticks * cfg.tick_size
            if mode == "Band width":
                return (float(row5["bb_upper"]) - float(row5["bb_lower"])) * mult
            if mode == "ATR":
                atr_val = float(row5.get("atr") or 0.0)
                if atr_val <= 0:
                    return None
                return atr_val * mult
            return None

        sl_dist = _distance_from_mode(cfg.sl_mode, cfg.sl_ticks, cfg.sl_mult)
        if sl_dist is None:
            _skip("sl_distance_invalid", direction, t, close1, entry_limit, cancel_levels)
            continue
        if direction == "LONG":
            sl_price = entry_price - sl_dist
        else:
            sl_price = entry_price + sl_dist

        tp_prices: List[float] = []
        if cfg.tp_mid5 and cfg.entry_mode != "Re-entry 5m":
            tp_prices.append(float(row5["bb_mid"]))
        if cfg.tp_mid15:
            tp_prices.append(float(row15["bb_mid"]))
        tp_prices.append(float(row5["bb_upper"]) if direction == "LONG" else float(row5["bb_lower"]))
        if cfg.tp_fixed:
            tp_dist = _distance_from_mode("Fixed ticks", cfg.tp_ticks, cfg.tp_mult)
            if tp_dist:
                tp_prices.append(entry_price + tp_dist if direction == "LONG" else entry_price - tp_dist)
        if cfg.tp_band:
            tp_dist = _distance_from_mode("Band width", cfg.tp_ticks, cfg.tp_mult)
            if tp_dist:
                tp_prices.append(entry_price + tp_dist if direction == "LONG" else entry_price - tp_dist)
        if cfg.tp_atr:
            tp_dist = _distance_from_mode("ATR", cfg.tp_ticks, cfg.tp_mult)
            if tp_dist:
                tp_prices.append(entry_price + tp_dist if direction == "LONG" else entry_price - tp_dist)

        if direction == "LONG":
            tp_prices = [p for p in tp_prices if p > entry_price]
        else:
            tp_prices = [p for p in tp_prices if p < entry_price]
        if not tp_prices:
            _skip("tp_targets_invalid", direction, t, close1, entry_limit, cancel_levels)
            continue

        exit_reason = "EOD"
        exit_price = entry_price
        exit_time = entry_time
        tp_hit_price = None
        entry_pos = idx1.searchsorted(entry_time, side="left")
        for j in range(entry_pos, len(df1)):
            bar = df1.iloc[j]
            bar_time = idx1[j]
            high = float(bar["high"]) if not pd.isna(bar.get("high")) else float(bar["close"])
            low = float(bar["low"]) if not pd.isna(bar.get("low")) else float(bar["close"])
            strategy_now = _strategy_for_time(pd.Timestamp(bar_time), cfg)
            if strategy_now != trade_strategy:
                exit_reason = "SWITCH"
                exit_price = float(bar["close"])
                exit_time = bar_time
                break
            if cfg.daily_profit_enabled:
                remaining = cfg.daily_profit_ticks - daily_realized_ticks
                if remaining <= 0:
                    exit_reason = "DAYCAP"
                    exit_price = entry_price
                    exit_time = bar_time
                    daily_lock = True
                    break
                if direction == "LONG":
                    max_unreal = (high - entry_price) / cfg.tick_size
                    if max_unreal >= remaining:
                        exit_reason = "DAYCAP"
                        exit_price = entry_price + (remaining * cfg.tick_size)
                        exit_time = bar_time
                        daily_lock = True
                        break
                else:
                    max_unreal = (entry_price - low) / cfg.tick_size
                    if max_unreal >= remaining:
                        exit_reason = "DAYCAP"
                        exit_price = entry_price - (remaining * cfg.tick_size)
                        exit_time = bar_time
                        daily_lock = True
                        break
            if direction == "LONG":
                hit_sl = low <= sl_price
                hit_tp = any(high >= tp for tp in tp_prices)
            else:
                hit_sl = high >= sl_price
                hit_tp = any(low <= tp for tp in tp_prices)

            if hit_sl and hit_tp:
                exit_reason = "SL"
                exit_price = sl_price
                exit_time = bar_time
                break
            if hit_sl:
                exit_reason = "SL"
                exit_price = sl_price
                exit_time = bar_time
                break
            if hit_tp:
                exit_reason = "TP"
                if direction == "SHORT":
                    hit_list = [tp for tp in tp_prices if low <= tp]
                    tp_hit_price = max(hit_list) if hit_list else None
                else:
                    hit_list = [tp for tp in tp_prices if high >= tp]
                    tp_hit_price = min(hit_list) if hit_list else None
                exit_price = tp_hit_price if tp_hit_price is not None else exit_price
                exit_time = bar_time
                break

        pnl_price = (exit_price - entry_price) if direction == "LONG" else (entry_price - exit_price)
        pnl_ticks = pnl_price / cfg.tick_size if cfg.tick_size else 0.0
        pnl_usd = pnl_ticks * cfg.tick_value * max(1, int(cfg.lot_size))
        trades.append(
            TradeResult(
                direction=direction,
                trigger_time=t.to_pydatetime(),
                trigger_close=close1,
                confirm_time=confirm_time,
                confirm_close=confirm_close,
                entry_limit=entry_limit,
                entry_time=entry_time.to_pydatetime(),
                entry_price=entry_price,
                exit_time=exit_time.to_pydatetime(),
                exit_price=exit_price,
                exit_reason=exit_reason,
                pnl_price=pnl_price,
                pnl_ticks=pnl_ticks,
                pnl_usd=pnl_usd,
                sl_price=sl_price,
                tp_prices=tp_prices,
                tp_hit_price=tp_hit_price,
            )
        )
        daily_realized_ticks += pnl_ticks
        if exit_reason == "SL" and cfg.cooldown_enabled:
            cooldown_seconds = cfg.cooldown_seconds_v2 if trade_strategy == "v2" else cfg.cooldown_seconds_v1
            cooldown_until = exit_time + pd.Timedelta(seconds=max(0, int(cooldown_seconds)))
        in_trade_until = exit_time

    wins = sum(1 for t in trades if t.exit_reason == "TP")
    losses = sum(1 for t in trades if t.exit_reason == "SL")
    total_pnl_ticks = sum(t.pnl_ticks for t in trades)
    total_pnl_price = sum(t.pnl_price for t in trades)
    total_pnl_usd = sum(t.pnl_usd for t in trades)
    avg_pnl_ticks = total_pnl_ticks / len(trades) if trades else 0.0
    win_rate = (wins / len(trades) * 100.0) if trades else 0.0
    max_consec_loss = 0.0
    current_loss = 0.0
    max_consec_start = None
    max_consec_end = None
    equity = 0.0
    equity_at_max_start = 0.0
    equity_at_max_end = 0.0
    current_loss_start_time = None
    equity_at_current_loss_start = 0.0
    for t in trades:
        equity += t.pnl_usd
        if t.pnl_usd < 0:
            if current_loss == 0.0:
                current_loss_start_time = t.exit_time
                equity_at_current_loss_start = equity - t.pnl_usd
            current_loss += abs(t.pnl_usd)
            if current_loss > max_consec_loss:
                max_consec_loss = current_loss
                max_consec_start = current_loss_start_time
                max_consec_end = t.exit_time
                equity_at_max_start = equity_at_current_loss_start
                equity_at_max_end = equity
        else:
            current_loss = 0.0
            current_loss_start_time = None

    # equity curve, drawdown, and trailing loss limit (trails until breakeven)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    dd_start = None
    dd_end = None
    peak_time = None
    min_equity = 0.0
    min_equity_time = None
    trailing_breached = False
    trailing_breach_time = None
    trailing_min_margin = float("inf")
    for t in trades:
        equity += t.pnl_usd
        if equity > peak:
            peak = equity
            peak_time = t.exit_time
        drawdown = peak - equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            dd_start = peak_time
            dd_end = t.exit_time
        if equity < min_equity:
            min_equity = equity
            min_equity_time = t.exit_time
        # trailing loss limit: max_equity - 4500, capped at 0 once max_equity >= 4500
        trail_floor = peak - 4500.0
        if trail_floor > 0.0:
            trail_floor = 0.0
        margin = equity - trail_floor
        if margin < trailing_min_margin:
            trailing_min_margin = margin
        if margin < 0 and not trailing_breached:
            trailing_breached = True
            trailing_breach_time = t.exit_time
    return BacktestResult(
        trades=trades,
        skipped_signals=skipped_signals,
        wins=wins,
        losses=losses,
        win_rate=win_rate,
        total_pnl_ticks=total_pnl_ticks,
        avg_pnl_ticks=avg_pnl_ticks,
        skipped=skipped,
        total_pnl_price=total_pnl_price,
        total_pnl_usd=total_pnl_usd,
        max_consecutive_loss_usd=max_consec_loss,
        max_consecutive_loss_start=max_consec_start,
        max_consecutive_loss_end=max_consec_end,
        equity_at_max_consec_start=equity_at_max_start,
        equity_at_max_consec_end=equity_at_max_end,
        max_drawdown_usd=max_drawdown,
        max_drawdown_start=dd_start,
        max_drawdown_end=dd_end,
        min_equity=min_equity,
        min_equity_time=min_equity_time,
        trailing_limit_breached=trailing_breached,
        trailing_limit_min_margin=trailing_min_margin if trailing_min_margin != float("inf") else 0.0,
        trailing_limit_breach_time=trailing_breach_time,
        data_start=data_start,
        data_end=data_end,
    )

def run_backtest(client: TopstepXClient, cfg: BacktestConfig) -> BacktestResult:
    data = _prepare_data(client, cfg)
    if data is None:
        return BacktestResult(
            trades=[],
            skipped_signals=[],
            wins=0,
            losses=0,
            win_rate=0.0,
            total_pnl_ticks=0.0,
            avg_pnl_ticks=0.0,
            skipped=0,
            total_pnl_price=0.0,
            total_pnl_usd=0.0,
            max_consecutive_loss_usd=0.0,
            max_consecutive_loss_start=None,
            max_consecutive_loss_end=None,
            equity_at_max_consec_start=0.0,
            equity_at_max_consec_end=0.0,
            max_drawdown_usd=0.0,
            max_drawdown_start=None,
            max_drawdown_end=None,
            min_equity=0.0,
            min_equity_time=None,
            trailing_limit_breached=False,
            trailing_limit_min_margin=0.0,
            trailing_limit_breach_time=None,
            data_start=None,
            data_end=None,
        )
    df15, df5, df1 = data
    return _run_backtest_core(df15, df5, df1, cfg)

def run_backtest_grid(
    client: TopstepXClient,
    base_cfg: BacktestConfig,
    *,
    parallel: bool = True,
    max_workers: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> tuple[List[GridResult], dict]:
    data = _prepare_data(client, base_cfg)
    if data is None:
        return [], {"grid_thinned": False, "sl_step": None, "tp_step": None}
    df15, df5, df1 = data

    bb_lens = list(range(8, 25, 2))  # 8..24 step 2
    bb_mults = [round(x, 1) for x in [1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0, 2.1, 2.2]]
    entry_modes = ["Immediate"]
    trend_modes = ["5m close vs midline", "Live price vs 5m midline"]
    cooldown_opts = [True, False]
    rsi_enabled_opts = [False, True]
    entry_offsets = list(range(10, 121, 10))
    entry_timeouts = list(range(10, 91, 10))
    target_combos = 2000
    grid_thinned = False

    def _count():
        return len(bb_lens) * len(bb_mults) * len(entry_modes) * len(trend_modes) * len(rsi_enabled_opts) * len(entry_offsets) * len(entry_timeouts) * len(cooldown_opts)

    while _count() > target_combos:
        grid_thinned = True
        lengths = {
            "bb_mults": len(bb_mults),
            "bb_lens": len(bb_lens),
            "entry_offsets": len(entry_offsets),
            "entry_timeouts": len(entry_timeouts),
        }
        largest = max(lengths, key=lengths.get)
        if largest == "bb_mults" and len(bb_mults) > 3:
            bb_mults = bb_mults[::2]
        elif largest == "bb_lens" and len(bb_lens) > 3:
            bb_lens = bb_lens[::2]
        elif largest == "entry_offsets" and len(entry_offsets) > 3:
            entry_offsets = entry_offsets[::2]
        elif largest == "entry_timeouts" and len(entry_timeouts) > 3:
            entry_timeouts = entry_timeouts[::2]
        else:
            break

    results: List[GridResult] = []
    configs: List[BacktestConfig] = []
    for bb_len in bb_lens:
        for bb_mult in bb_mults:
            for entry_mode in entry_modes:
                for trend_filter_mode in trend_modes:
                    for rsi_enabled in rsi_enabled_opts:
                        for entry_offset_ticks in entry_offsets:
                            for entry_timeout_min in entry_timeouts:
                                for cooldown_enabled in cooldown_opts:
                                    cfg = replace(
                                        base_cfg,
                                        bb_len=bb_len,
                                        bb_mult=bb_mult,
                                        entry_mode=entry_mode,
                                        trend_filter_mode=trend_filter_mode,
                                        rsi_enabled=rsi_enabled,
                                        entry_offset_ticks=entry_offset_ticks,
                                        entry_timeout_min=entry_timeout_min,
                                        cooldown_enabled=cooldown_enabled,
                                    )
                                    configs.append(cfg)

    total = len(configs)
    completed = 0
    def _report():
        if progress_callback:
            progress_callback(completed, total)

    if parallel and len(configs) > 1:
        workers = max_workers or min(8, os.cpu_count() or 1)
        if workers < 1:
            workers = 1
        with cf.ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker,
            initargs=(df15, df5, df1),
        ) as ex:
            futures = [ex.submit(_run_one, cfg) for cfg in configs]
            for fut in cf.as_completed(futures):
                results.append(fut.result())
                completed += 1
                _report()
        return results, {"grid_thinned": grid_thinned, "sl_step": None, "tp_step": None, "parallel": True, "workers": workers, "total": total}

    for cfg in configs:
        res = _run_backtest_core(df15, df5, df1, cfg)
        results.append(
            GridResult(
                cfg=cfg,
                trades=len(res.trades),
                wins=res.wins,
                losses=res.losses,
                win_rate=res.win_rate,
                total_pnl_ticks=res.total_pnl_ticks,
                total_pnl_usd=res.total_pnl_usd,
                skipped=res.skipped,
                max_consecutive_loss_usd=res.max_consecutive_loss_usd,
                data_start=res.data_start,
                data_end=res.data_end,
            )
        )
        completed += 1
        _report()
    return results, {"grid_thinned": grid_thinned, "sl_step": None, "tp_step": None, "parallel": False, "workers": 1, "total": total}

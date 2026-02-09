from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .api_client import TopstepXClient
from .market_hub import MarketHubClient, Quote
from .signal_engine import SignalEngine, get_bands_snapshot, bars_to_df
from .indicators import atr


@dataclass
class TradeState:
    direction: str
    entry_price: float | None
    entry_limit: float | None
    sl_price: float | None
    tp_prices: list[float]
    entry_time: datetime | None
    pending_since: datetime | None
    pending_tp_cancel: list[float]
    status: str  # "pending_confirm", "pending_entry", "active", "closed"
    entry_mode: str
    confirm_price: float | None
    sl_mode: str
    tick_size: float


class TradingLoopV1:
    def __init__(
        self,
        *,
        client: TopstepXClient,
        log: Callable[[str], None],
        get_settings: Callable[[], dict],
        get_contract_id: Callable[[], str],
        get_symbol_id: Callable[[], str],
        get_account_id: Callable[[], int],
        update_trade_display: Callable[[TradeState | None], None],
        set_last_price: Callable[[float], None],
        is_daily_locked: Optional[Callable[[], bool]] = None,
    ):
        self.client = client
        self.log = log
        self.get_settings = get_settings
        self.get_contract_id = get_contract_id
        self.get_symbol_id = get_symbol_id
        self.get_account_id = get_account_id
        self.update_trade_display = update_trade_display
        self.set_last_price = set_last_price
        self.is_daily_locked = is_daily_locked

        self._trade_lock = threading.Lock()
        self._trade: TradeState | None = None
        self._order_inflight = False
        self._position_lock = False
        self._cooldown_until: datetime | None = None

        self._last_live_price: float | None = None
        self._last_quote_ts: float | None = None
        self._live_feed_enabled = False
        self._last_reconnect_ts: float | None = None
        self._last_ui_update_ts: float = 0.0

        self._last_s5_snapshot_ts: float = 0.0
        self._last_s5_snapshot = None
        self._last_s5_key: tuple | None = None

        self._market: MarketHubClient | None = None
        self._signal_engine: SignalEngine | None = None
        self._last_flatten_ts: float = 0.0

    # ---------- trade state ----------
    def _set_trade(self, trade: TradeState | None):
        with self._trade_lock:
            self._trade = trade

    def _get_trade(self) -> TradeState | None:
        with self._trade_lock:
            return self._trade

    def _set_order_inflight(self, val: bool):
        with self._trade_lock:
            self._order_inflight = val

    def _get_order_inflight(self) -> bool:
        with self._trade_lock:
            return self._order_inflight

    def _set_position_lock(self, val: bool):
        with self._trade_lock:
            self._position_lock = val

    def _get_position_lock(self) -> bool:
        with self._trade_lock:
            return self._position_lock

    # ---------- time window ----------
    def _within_window_utc(self, enabled: bool, windows_str: str) -> bool:
        try:
            if not enabled:
                return True
            windows_str = str(windows_str or "").strip()
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
                start = datetime.now(timezone.utc).replace(hour=start_h, minute=start_m, second=0, microsecond=0).time()
                end = datetime.now(timezone.utc).replace(hour=end_h, minute=end_m, second=0, microsecond=0).time()
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

    def start_live_watchdog(self):
        def _loop():
            while True:
                try:
                    if not self._live_feed_enabled or not self._market:
                        time.sleep(5)
                        continue
                    settings = self.get_settings()
                    if not self._within_window_utc(
                        bool(settings.get("trade_window_enabled", False)),
                        str(settings.get("trade_windows", "")),
                    ):
                        time.sleep(10)
                        continue
                    now = time.time()
                    last = self._last_quote_ts or 0.0
                    if last and now - last > 120:
                        last_reconnect = self._last_reconnect_ts or 0.0
                        if now - last_reconnect > 120:
                            self._last_reconnect_ts = now
                            try:
                                contract_id = self.get_contract_id()
                                sym = self.get_symbol_id()
                            except Exception:
                                time.sleep(10)
                                continue
                            self.log("Live feed stale. Reconnecting...")
                            try:
                                self._market.stop()
                            except Exception:
                                pass
                            self._market.start()
                            self._market.subscribe_contract(contract_id)
                            self.log(f"Live feed restarted. contractId={contract_id} symbolId={sym}")
                    time.sleep(5)
                except Exception:
                    time.sleep(5)

        threading.Thread(target=_loop, daemon=True).start()

    # ---------- live feed ----------
    def start_live_feed(self):
        try:
            contract_id = self.get_contract_id()
            sym = self.get_symbol_id()
        except Exception:
            return
        if not self.client.token:
            return
        if not self._market:
            self._market = MarketHubClient(token_factory=lambda: self.client.token or "", log=self.log)
            self._market.set_on_quote(self._on_quote)
        self._market.start()
        self._market.subscribe_contract(contract_id)
        self._live_feed_enabled = True
        self.log(f"Live feed started. contractId={contract_id} symbolId={sym}")

    def stop_live_feed(self):
        if self._market:
            self._market.stop()
        self._live_feed_enabled = False
        self.log("Live feed stop requested.")

    # ---------- execution ----------
    def _place_live_order(self, direction: str, entry_price: float, sl_price: float, tp_prices: list[float]):
        settings = self.get_settings()
        if not settings.get("exec_enabled", False):
            return
        try:
            account_id = self.get_account_id()
            contract_id = self.get_contract_id()
            size = int(settings.get("order_size", 0))
            if size <= 0:
                raise ValueError("Order size must be > 0")
            # Per docs: 0 = Buy, 1 = Sell
            side = 0 if direction == "LONG" else 1
            tag = f"auto_{int(time.time() * 1000)}"
            data = self.client.place_order(
                account_id=account_id,
                contract_id=contract_id,
                side=side,
                size=size,
                stop_ticks=None,  # SL handled by live flatten
                take_profit_ticks=None,  # TP handled by live flatten
                custom_tag=tag,
            )
            order_id = data.get("orderId")
            self.log(
                f"Order placed. orderId={order_id} tag={tag} side={side} size={size} "
                f"stopTicks=None tpTicks=None"
            )
            self._set_position_lock(True)
        except Exception as e:
            self.log(f"Order place error: {e}")
            self._set_trade(None)
            self.update_trade_display(None)
            self._set_order_inflight(False)
            self._set_position_lock(False)

    def _flatten_market(self, direction: str):
        settings = self.get_settings()
        if not settings.get("exec_enabled", False):
            return
        try:
            account_id = self.get_account_id()
            contract_id = self.get_contract_id()
            size = int(settings.get("order_size", 0))
            if size <= 0:
                raise ValueError("Order size must be > 0")
            side = 1 if direction == "LONG" else 0
            tag = f"flat_{int(time.time() * 1000)}"
            data = self.client.place_order(
                account_id=account_id,
                contract_id=contract_id,
                side=side,
                size=size,
                stop_ticks=None,
                take_profit_ticks=None,
                custom_tag=tag,
            )
            order_id = data.get("orderId")
            self.log(f"Flatten sent. orderId={order_id} tag={tag} side={side} size={size}")
            self._set_position_lock(False)
            self._cancel_open_orders_for_contract(account_id, contract_id)
        except Exception as e:
            self.log(f"Flatten error: {e}")

    def _cancel_open_orders_for_contract(self, account_id: int, contract_id: str):
        try:
            data = self.client.search_open_orders(account_id=account_id)
            orders = data.get("orders") or []
            for o in orders:
                try:
                    if str(o.get("contractId")) != str(contract_id):
                        continue
                    order_id = o.get("id") or o.get("orderId")
                    if order_id is None:
                        continue
                    self.client.cancel_order(account_id=account_id, order_id=int(order_id))
                    self.log(f"Cancelled open order: orderId={order_id}")
                except Exception as e:
                    self.log(f"Cancel order error: {e}")
        except Exception as e:
            self.log(f"Search open orders error: {e}")

    def flatten_active(self, reason: str = "external_lock"):
        trade = self._get_trade()
        if not trade:
            return
        now_ts = time.time()
        if now_ts - self._last_flatten_ts < 1.0:
            return
        self._last_flatten_ts = now_ts
        self.log(f"Flatten requested: {reason}.")
        self._flatten_market(trade.direction)
        self._set_trade(None)
        self.update_trade_display(None)

    # ---------- targets ----------
    def _compute_targets(self, direction: str, entry_price: float, entry_mode: str) -> tuple[float | None, list[float]]:
        settings = self.get_settings()
        sl_mode = settings.get("sl_mode")
        tick_size = float(settings.get("tick_size", 0.0))
        sl_ticks = int(settings.get("sl_ticks", 0))
        tp_ticks = int(settings.get("tp_ticks", 0))
        sl_mult = float(settings.get("sl_mult", 0.0))
        tp_mult = float(settings.get("tp_mult", 0.0))
        contract_id = self.get_contract_id()
        live = bool(settings.get("live", False))

        bb_len = int(settings.get("bb_len", 0))
        bb_mult = float(settings.get("bb_mult", 0.0))
        s5 = get_bands_snapshot(
            self.client,
            contract_id,
            "5m",
            live=live,
            length=bb_len,
            mult=bb_mult,
            limit=400,
        )
        if not s5:
            self.log("TP/SL calc failed: no 5m snapshot.")
            return None, []

        s15 = None
        if settings.get("tp_mid15"):
            s15 = get_bands_snapshot(
                self.client,
                contract_id,
                "15m",
                live=live,
                length=bb_len,
                mult=bb_mult,
                limit=400,
            )
            if not s15:
                self.log("TP calc failed: no 15m snapshot.")
                return None, []

        atr_value = None
        if sl_mode == "ATR" or settings.get("tp_atr"):
            data = self.client.retrieve_bars(contract_id, "5m", limit=400, live=live, include_partial_bar=False)
            bars = data.get("bars") or data.get("candles") or data.get("data") or []
            df = bars_to_df(bars)
            if df.empty or df["high"].isna().all() or df["low"].isna().all():
                self.log("ATR calc failed: insufficient 5m bars.")
                return None, []
            atr_value = atr(df["high"], df["low"], df["close"], length=14)

        def _distance_from_mode(mode: str, ticks: int, mult: float) -> float | None:
            if mode == "Fixed ticks":
                return ticks * tick_size
            if mode == "Band width":
                return (s5.upper - s5.lower) * mult
            if mode == "ATR":
                if atr_value is None:
                    return None
                return atr_value * mult
            return None

        sl_dist = _distance_from_mode(sl_mode, sl_ticks, sl_mult)
        if sl_dist is None or sl_dist <= 0:
            self.log("SL calc failed: invalid SL distance.")
            return None, []

        sl_price = entry_price - sl_dist if direction == "LONG" else entry_price + sl_dist

        tp_prices: list[float] = []
        if settings.get("tp_mid5") and entry_mode != "Re-entry 5m":
            tp_prices.append(float(s5.mid))
        if settings.get("tp_mid15") and s15 is not None:
            tp_prices.append(float(s15.mid))
        if settings.get("tp_fixed"):
            tp_dist = _distance_from_mode("Fixed ticks", tp_ticks, tp_mult)
            if tp_dist and tp_dist > 0:
                tp_prices.append(entry_price + tp_dist if direction == "LONG" else entry_price - tp_dist)
        if settings.get("tp_band"):
            tp_dist = _distance_from_mode("Band width", tp_ticks, tp_mult)
            if tp_dist and tp_dist > 0:
                tp_prices.append(entry_price + tp_dist if direction == "LONG" else entry_price - tp_dist)
        if settings.get("tp_atr"):
            tp_dist = _distance_from_mode("ATR", tp_ticks, tp_mult)
            if tp_dist and tp_dist > 0:
                tp_prices.append(entry_price + tp_dist if direction == "LONG" else entry_price - tp_dist)

        if direction == "LONG":
            tp_prices = [p for p in tp_prices if p > entry_price]
        else:
            tp_prices = [p for p in tp_prices if p < entry_price]
        if not tp_prices:
            self.log("TP calc failed: no valid TP targets.")
            return None, []

        tp_prices = sorted(set(tp_prices)) if direction == "LONG" else sorted(set(tp_prices), reverse=True)
        return sl_price, tp_prices

    # ---------- signals ----------
    def start_signals(self):
        contract_id = self.get_contract_id()
        if self._signal_engine:
            self._signal_engine.stop()
            self._signal_engine = None

        def on_signal(direction: str, payload: dict):
            with self._trade_lock:
                if self.is_daily_locked and self.is_daily_locked():
                    self.log("Signal ignored: daily profit target reached.")
                    return
                if self._cooldown_active():
                    self.log("Signal ignored: cooldown active after stoploss.")
                    return
                if self._trade is not None or self._order_inflight or self._position_lock:
                    self.log("Signal ignored: trade already active or pending.")
                    return
                self._order_inflight = True
            try:
                trigger_price = float(payload.get("trigger_price"))
            except Exception:
                trigger_price = None
            entry_mode = "Immediate"
            confirm_price = None
            if trigger_price is None:
                trigger_price = self._last_live_price
            if trigger_price is None:
                self.log("Signal ignored: no live trigger price available.")
                self._set_order_inflight(False)
                return

            trade = TradeState(
                direction=direction,
                entry_price=trigger_price,
                entry_limit=None,
                sl_price=None,
                tp_prices=[],
                entry_time=datetime.now(timezone.utc),
                pending_since=None,
                pending_tp_cancel=[],
                status="active",
                entry_mode=entry_mode,
                confirm_price=confirm_price,
                sl_mode=str(self.get_settings().get("sl_mode")),
                tick_size=float(self.get_settings().get("tick_size", 0.0)),
            )
            self._set_trade(trade)
            self._set_position_lock(True)
            self.update_trade_display(trade)
            self.log(f"Signal emitted: {direction}. Market entry at {trigger_price:.2f}. Calculating TP/SL...")
            self.start_live_feed()

            def calc_worker():
                try:
                    sl, tps = self._compute_targets(trade.direction, trade.entry_price or 0.0, trade.entry_mode)
                    if sl is None or not tps:
                        self.log("TP/SL calc failed. Trade cancelled.")
                        self._set_trade(None)
                        self.update_trade_display(None)
                        self._set_order_inflight(False)
                        self._set_position_lock(False)
                        return
                    trade.sl_price = sl
                    trade.tp_prices = tps
                    self._set_trade(trade)
                    self.update_trade_display(trade)
                    preview = ",".join(f"{p:.2f}" for p in tps[:3])
                    self.log(f"Targets set: SL={sl:.2f} TP={preview}")
                    self._place_live_order(trade.direction, trade.entry_price or 0.0, sl, tps)
                    self._set_order_inflight(False)
                except Exception as e:
                    self.log(f"TP/SL calc error: {e}")
                    self._set_trade(None)
                    self.update_trade_display(None)
                    self._set_order_inflight(False)
                    self._set_position_lock(False)

            threading.Thread(target=calc_worker, daemon=True).start()

        settings = self.get_settings()
        self._signal_engine = SignalEngine(
            client=self.client,
            contract_id=contract_id,
            live=bool(settings.get("live", False)),
            offset_seconds=int(settings.get("offset", 0)),
            log=self.log,
            on_signal=on_signal,
            get_live_price=lambda: self._last_live_price,
            bb_len=int(settings.get("bb_len", 0)),
            bb_mult=float(settings.get("bb_mult", 0.0)),
            rsi_enabled=bool(settings.get("rsi_enabled", False)),
            rsi_len=int(settings.get("rsi_len", 14)),
            rsi_overbought=float(settings.get("rsi_overbought", 70.0)),
            rsi_oversold=float(settings.get("rsi_oversold", 30.0)),
            retrace_ticks=int(settings.get("retrace_ticks", 0)),
            tick_size=float(settings.get("tick_size", 0.0)),
            trend_filter_mode=str(settings.get("trend_filter_mode", "")),
            trade_window_enabled=bool(settings.get("trade_window_enabled", False)),
            trade_windows=str(settings.get("trade_windows", "")),
        )
        self._signal_engine.start()
        self.log("Signal engine started.")

    def stop_signals(self):
        if self._signal_engine:
            self._signal_engine.stop()
            self._signal_engine = None
        self.log("Signal engine stop requested.")

    # ---------- quote handling ----------
    def _cooldown_active(self) -> bool:
        settings = self.get_settings()
        if not settings.get("cooldown_enabled", False):
            return False
        if not self._cooldown_until:
            return False
        return datetime.now(timezone.utc) < self._cooldown_until

    def _on_quote(self, q: Quote):
        now_ts = time.time()
        settings = self.get_settings()
        if q.last_price is not None:
            self._last_live_price = float(q.last_price)
            self._last_quote_ts = now_ts
            if now_ts - self._last_ui_update_ts >= 0.2:
                self._last_ui_update_ts = now_ts
                self.set_last_price(float(q.last_price))
        trade = self._get_trade()
        if not trade or q.last_price is None:
            return

        if trade.status == "pending_entry":
            limit = trade.entry_limit
            price = float(q.last_price)
            now = datetime.now(timezone.utc)
            if trade.pending_since is not None:
                timeout_min = int(settings.get("entry_timeout_min", 0))
                if timeout_min > 0 and now - trade.pending_since > timedelta(minutes=timeout_min):
                    self.log("Entry cancelled: limit not filled within timeout.")
                    self._set_trade(None)
                    self.update_trade_display(None)
                    return
            can_fill = False
            if limit is None:
                can_fill = True
                fill_price = price
            else:
                if trade.direction == "LONG" and price <= limit:
                    can_fill = True
                    fill_price = limit
                elif trade.direction == "SHORT" and price >= limit:
                    can_fill = True
                    fill_price = limit
                else:
                    if trade.pending_tp_cancel:
                        if trade.direction == "LONG":
                            if any(price >= lvl for lvl in trade.pending_tp_cancel):
                                self.log("Entry cancelled: midline TP hit before entry fill.")
                                self._set_trade(None)
                                self.update_trade_display(None)
                                return
                        else:
                            if any(price <= lvl for lvl in trade.pending_tp_cancel):
                                self.log("Entry cancelled: midline TP hit before entry fill.")
                                self._set_trade(None)
                                self.update_trade_display(None)
                                return
                    return
            trade.entry_price = float(fill_price)
            trade.entry_time = datetime.now(timezone.utc)
            trade.status = "active"
            self._set_trade(trade)
            self.log(f"Entry set at {trade.entry_price:.2f} ({trade.direction}). Calculating TP/SL...")

            def calc_worker():
                try:
                    sl, tps = self._compute_targets(trade.direction, trade.entry_price or 0.0, trade.entry_mode)
                    if sl is None or not tps:
                        self.log("TP/SL calc failed. Trade cancelled.")
                        self._set_trade(None)
                        self.update_trade_display(None)
                        return
                    trade.sl_price = sl
                    trade.tp_prices = tps
                    self._set_trade(trade)
                    self.update_trade_display(trade)
                    preview = ",".join(f"{p:.2f}" for p in tps[:3])
                    self.log(f"Targets set: SL={sl:.2f} TP={preview}")
                    self._place_live_order(trade.direction, trade.entry_price or 0.0, sl, tps)
                except Exception as e:
                    self.log(f"TP/SL calc error: {e}")
                    self._set_trade(None)
                    self.update_trade_display(None)

            threading.Thread(target=calc_worker, daemon=True).start()
            return

        if trade.status != "active" or trade.sl_price is None or not trade.tp_prices:
            return

        price = float(q.last_price)
        hit_sl = hit_tp = False
        hit_tp_price = None
        live_band_price = None
        try:
            live = bool(settings.get("live", False))
            bb_len = int(settings.get("bb_len", 10))
            bb_mult = float(settings.get("bb_mult", 1.5))
            contract_id = self.get_contract_id()
            key = (contract_id, live, bb_len, bb_mult)
            if self._last_s5_snapshot is None or self._last_s5_key != key or (now_ts - self._last_s5_snapshot_ts) >= 5.0:
                self._last_s5_snapshot = get_bands_snapshot(
                    self.client,
                    contract_id,
                    "5m",
                    live=live,
                    length=bb_len,
                    mult=bb_mult,
                    limit=400,
                )
                self._last_s5_snapshot_ts = now_ts
                self._last_s5_key = key
            s5_live = self._last_s5_snapshot
            if s5_live:
                live_band_price = float(s5_live.upper) if trade.direction == "LONG" else float(s5_live.lower)
        except Exception:
            live_band_price = None

        if trade.direction == "LONG":
            hit_sl = price <= trade.sl_price
            hit_candidates = [tp for tp in trade.tp_prices if price >= tp]
            if live_band_price is not None and price >= live_band_price:
                hit_candidates.append(live_band_price)
            if hit_candidates:
                hit_tp = True
                hit_tp_price = min(hit_candidates)
        else:
            hit_sl = price >= trade.sl_price
            hit_candidates = [tp for tp in trade.tp_prices if price <= tp]
            if live_band_price is not None and price <= live_band_price:
                hit_candidates.append(live_band_price)
            if hit_candidates:
                hit_tp = True
                hit_tp_price = max(hit_candidates)

        if hit_sl and hit_tp:
            hit_tp = False

        if hit_sl or hit_tp:
            reason = "SL" if hit_sl else "TP"
            if reason == "TP" and hit_tp_price is not None:
                self.log(f"Exit {reason} at {price:.2f} (target {hit_tp_price:.2f})")
            else:
                self.log(f"Exit {reason} at {price:.2f}")
            if hit_sl:
                if settings.get("cooldown_enabled", False):
                    seconds = max(0, int(settings.get("cooldown_seconds", 0)))
                    self._cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
                    self.log(f"Cooldown started: {seconds} seconds after stoploss.")
                self._flatten_market(trade.direction)
            if hit_tp:
                self._flatten_market(trade.direction)
            self._set_trade(None)
            self.update_trade_display(None)

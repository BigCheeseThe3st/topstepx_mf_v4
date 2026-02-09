from __future__ import annotations

import os
import threading
import queue
import time
from datetime import datetime, timedelta, timezone, time as dt_time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from topstepx.api_client import TopstepXClient, ApiError
from topstepx.trading_loop_v1 import TradingLoopV1, TradeState
from topstepx.trading_loop_v2 import TradingLoopV2
from topstepx.strategy_switcher import StrategySwitcher, SwitchWindow
from topstepx.pnl_tracker import PnLTracker
from topstepx.backtester import BacktestConfig, run_backtest, run_backtest_grid
from topstepx.news_calendar import CalendarEvent, load_events, save_events, is_blocked

APP_TITLE = "TopstepX HF - v4 (Switch)"
DAILY_PROFIT_TICKS = 18000
DAILY_RESET_LOCAL_HOUR = 23
DAILY_RESET_LOCAL_MINUTE = 0

def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1100x700")

        self.client = TopstepXClient()
        self.session_token = None

        self.accounts = []  # list of dicts
        self.contracts = []  # list of dicts

        self._last_backtest = None
        self._log_queue: "queue.SimpleQueue[str]" = queue.SimpleQueue()
        self._last_price_value: float | None = None

        # UI state vars
        self.var_username = tk.StringVar()
        self.var_apikey = tk.StringVar()
        self.var_live = tk.BooleanVar(value=False)
        self.var_offset = tk.IntVar(value=3)
        self.var_sl_mode = tk.StringVar(value="Fixed ticks")
        self.var_tp_mid5 = tk.BooleanVar(value=True)
        self.var_tp_mid15 = tk.BooleanVar(value=True)
        self.var_tp_fixed = tk.BooleanVar(value=True)
        self.var_tp_band = tk.BooleanVar(value=False)
        self.var_tp_atr = tk.BooleanVar(value=False)
        self.var_sl_ticks = tk.IntVar(value=50)
        self.var_tp_ticks = tk.IntVar(value=100)
        self.var_sl_mult = tk.DoubleVar(value=0.5)
        self.var_tp_mult = tk.DoubleVar(value=1.0)
        self.var_tick_size = tk.DoubleVar(value=0.1)
        self.var_tick_value = tk.DoubleVar(value=10.0)
        self.var_backtest_lot_size = tk.IntVar(value=1)
        self.var_entry_offset_ticks = tk.IntVar(value=50)
        self.var_entry_timeout_min = tk.IntVar(value=10)
        self.var_entry_mode = tk.StringVar(value="Immediate")
        self.var_cancel_on_midline = tk.BooleanVar(value=False)
        self.var_bb_len = tk.IntVar(value=8)
        self.var_bb_mult = tk.DoubleVar(value=2.0)
        self.var_rsi_enabled = tk.BooleanVar(value=False)
        self.var_rsi_len = tk.IntVar(value=14)
        self.var_rsi_overbought = tk.DoubleVar(value=70.0)
        self.var_rsi_oversold = tk.DoubleVar(value=30.0)
        self.var_trend_filter_mode = tk.StringVar(value="5m close vs midline")
        self.var_retrace_ticks = tk.IntVar(value=2)
        self.var_trade_window_enabled = tk.BooleanVar(value=True)
        self.var_trade_windows_v1 = tk.StringVar(value="00:00-05:30")
        self.var_trade_windows_v2 = tk.StringVar(value="08:30-16:00")
        self.var_cooldown_enabled = tk.BooleanVar(value=True)
        self.var_cooldown_v1_seconds = tk.IntVar(value=55)
        self.var_cooldown_v2_seconds = tk.IntVar(value=120)
        self.var_exec_enabled = tk.BooleanVar(value=True)
        self.var_order_size = tk.IntVar(value=1)
        self.var_backtest_years = tk.IntVar(value=1)
        self.var_calendar_enabled = tk.BooleanVar(value=True)
        self.var_calendar_date = tk.StringVar(value="")
        self.var_calendar_time = tk.StringVar(value="")
        self.var_calendar_name = tk.StringVar(value="")
        self.var_calendar_pre_min = tk.IntVar(value=15)
        self.var_calendar_post_min = tk.IntVar(value=15)

        self.var_account = tk.StringVar()
        self.var_contract = tk.StringVar()
        self.var_symbol = tk.StringVar()

        self.var_last_price = tk.StringVar(value="NA")
        self.var_state = tk.StringVar(value="FLAT")
        self.var_entry_sl_tp = tk.StringVar(value="NA")
        self.var_daily_pnl = tk.StringVar(value="Daily ticks: 0.0 (R: 0.0 U: 0.0)")

        self._calendar_path = os.path.join(os.path.dirname(__file__), "calendar.json")
        self._calendar_events: list[CalendarEvent] = load_events(self._calendar_path)

        self._build_ui()
        self._start_log_flush()
        self._loop_v1 = TradingLoopV1(
            client=self.client,
            log=self.log,
            get_settings=self._get_trading_settings_v1,
            get_contract_id=self._selected_contract_id,
            get_symbol_id=self._selected_symbol_id,
            get_account_id=self._selected_account_id,
            update_trade_display=self._update_trade_display,
            set_last_price=self._set_last_price_from_loop,
            is_daily_locked=self._is_daily_locked,
        )
        self._loop_v2 = TradingLoopV2(
            client=self.client,
            log=self.log,
            get_settings=self._get_trading_settings_v2,
            get_contract_id=self._selected_contract_id,
            get_symbol_id=self._selected_symbol_id,
            get_account_id=self._selected_account_id,
            update_trade_display=self._update_trade_display,
            set_last_price=self._set_last_price_from_loop,
            is_blocked=self._calendar_block_reason,
            is_daily_locked=self._is_daily_locked,
        )
        self._switcher = StrategySwitcher(
            log=self.log,
            loop_v1=self._loop_v1,
            loop_v2=self._loop_v2,
            window=SwitchWindow(start=dt_time(8, 30), end=dt_time(16, 0)),
        )
        self._switcher.start()
        self._pnl_tracker = PnLTracker(
            token_factory=lambda: self.client.token or "",
            log=self.log,
            get_account_id=self._selected_account_id,
            get_tick_value=lambda: float(self.var_tick_value.get()),
            get_tick_size=lambda: float(self.var_tick_size.get()),
            get_last_price=lambda: self._last_price_value,
            update_daily_pnl=self._update_daily_pnl_from_tracker,
            daily_profit_ticks=float(DAILY_PROFIT_TICKS),
            daily_reset_hour=int(DAILY_RESET_LOCAL_HOUR),
            daily_reset_minute=int(DAILY_RESET_LOCAL_MINUTE),
            storage_path=os.path.join(os.path.dirname(__file__), "pnl_state.json"),
            poll_interval_sec=300,
        )
        self._pnl_tracker.set_on_lock(lambda reason: self._switcher.flatten_active(reason))
        self._pnl_tracker.start()
        self._switcher.start_live_watchdog()

    # ---------- logging ----------
    def log(self, msg: str):
        line = f"{utc_ts()} | {msg}"
        self._log_queue.put(line)
        print(line, flush=True)

    def _start_log_flush(self):
        def _flush():
            try:
                while True:
                    line = self._log_queue.get_nowait()
                    self.txt_log.configure(state="normal")
                    self.txt_log.insert("end", line + "\n")
                    self.txt_log.see("end")
                    self.txt_log.configure(state="disabled")
            except Exception:
                pass
            self.after(200, _flush)
        self.after(200, _flush)

    # ---------- UI ----------
    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)

        self.tab_conn = ttk.Frame(nb)
        self.tab_sig = ttk.Frame(nb)
        self.tab_cal = ttk.Frame(nb)
        nb.add(self.tab_conn, text="Connection")
        nb.add(self.tab_sig, text="Signals / Live Feed")
        nb.add(self.tab_cal, text="Calendar")

        self._build_conn_tab()
        self._build_sig_tab()
        self._build_calendar_tab()

    def _build_conn_tab(self):
        frm = ttk.Frame(self.tab_conn, padding=12)
        frm.pack(fill="both", expand=True)

        r = 0
        ttk.Label(frm, text="Username").grid(row=r, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_username, width=30).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="API Key").grid(row=r, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.var_apikey, width=50, show="*").grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Checkbutton(frm, text="Live", variable=self.var_live).grid(row=r, column=1, sticky="w")
        r += 1

        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=2, sticky="w", pady=(10,0))
        ttk.Button(btns, text="Login", command=self.login).pack(side="left", padx=4)
        # Disable downstream actions until we've successfully logged in.
        self.btn_load_accounts = ttk.Button(btns, text="Load Accounts", command=self.load_accounts, state="disabled")
        self.btn_load_accounts.pack(side="left", padx=4)
        self.btn_load_contracts = ttk.Button(btns, text="Load Contracts", command=self.load_contracts, state="disabled")
        self.btn_load_contracts.pack(side="left", padx=4)

        frm.grid_columnconfigure(1, weight=1)

        note = ttk.Label(frm, text="Tip: Load accounts/contracts after login. Contract dropdown appears in Signals tab.", foreground="#888")
        note.grid(row=r+1, column=0, columnspan=2, sticky="w", pady=(10,0))

    def _build_sig_tab(self):
        top = ttk.Frame(self.tab_sig, padding=12)
        top.pack(fill="both", expand=True)

        mon = ttk.LabelFrame(top, text="Trade Monitor", padding=10)
        mon.pack(fill="x")

        row = ttk.Frame(mon)
        row.pack(fill="x")
        ttk.Label(row, text="State:").pack(side="left")
        ttk.Label(row, textvariable=self.var_state, width=10).pack(side="left", padx=(4,16))
        ttk.Label(row, text="Last price:").pack(side="left")
        ttk.Label(row, textvariable=self.var_last_price, width=14).pack(side="left", padx=(4,16))
        ttk.Label(row, text="Entry/SL/TP:").pack(side="left")
        ttk.Label(row, textvariable=self.var_entry_sl_tp).pack(side="left", padx=(4,0))
        pnl_row = ttk.Frame(mon)
        pnl_row.pack(fill="x", pady=(4,0))
        ttk.Label(pnl_row, text="Daily PnL:").pack(side="left")
        ttk.Label(pnl_row, textvariable=self.var_daily_pnl).pack(side="left", padx=(4,0))

        controls = ttk.LabelFrame(top, text="Controls", padding=10)
        controls.pack(fill="x", pady=(10,0))

        grid = ttk.Frame(controls)
        grid.pack(fill="x")

        ttk.Label(grid, text="Account").grid(row=0, column=0, sticky="w")
        self.cmb_account = ttk.Combobox(grid, textvariable=self.var_account, width=35, state="readonly", values=[])
        self.cmb_account.grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(grid, text="Contract (auto)").grid(row=1, column=0, sticky="w")
        self.cmb_contract = ttk.Combobox(grid, textvariable=self.var_contract, width=35, state="disabled", values=[])
        self.cmb_contract.grid(row=1, column=1, sticky="w", padx=6)
        self.cmb_contract.bind("<<ComboboxSelected>>", self._on_contract_selected)

        ttk.Label(grid, text="SymbolId").grid(row=2, column=0, sticky="w")
        ttk.Entry(grid, textvariable=self.var_symbol, width=20, state="readonly").grid(row=2, column=1, sticky="w", padx=6)

        ttk.Label(grid, text="1m offset seconds").grid(row=0, column=2, sticky="w", padx=(20,0))
        self.spn_offset = ttk.Spinbox(grid, from_=0, to=59, textvariable=self.var_offset, width=6, state="disabled")
        self.spn_offset.grid(row=0, column=3, sticky="w")

        ttk.Label(grid, text="Tick size").grid(row=1, column=2, sticky="w", padx=(20,0))
        self.ent_tick_size = ttk.Entry(grid, textvariable=self.var_tick_size, width=8, state="disabled")
        self.ent_tick_size.grid(row=1, column=3, sticky="w")

        ttk.Label(grid, text="Tick value ($)").grid(row=2, column=2, sticky="w", padx=(20,0))
        self.ent_tick_value = ttk.Entry(grid, textvariable=self.var_tick_value, width=8)
        self.ent_tick_value.grid(row=2, column=3, sticky="w")
        ttk.Label(grid, text="Backtest lot size").grid(row=3, column=0, sticky="w")
        self.ent_backtest_lot = ttk.Entry(grid, textvariable=self.var_backtest_lot_size, width=8)
        self.ent_backtest_lot.grid(row=3, column=1, sticky="w", padx=6)

        ttk.Label(grid, text="Entry offset ticks").grid(row=3, column=2, sticky="w", padx=(20,0))
        self.ent_entry_offset = ttk.Entry(grid, textvariable=self.var_entry_offset_ticks, width=8, state="disabled")
        self.ent_entry_offset.grid(row=3, column=3, sticky="w")

        ttk.Label(grid, text="Entry timeout min").grid(row=4, column=2, sticky="w", padx=(20,0))
        self.ent_entry_timeout = ttk.Entry(grid, textvariable=self.var_entry_timeout_min, width=8, state="disabled")
        self.ent_entry_timeout.grid(row=4, column=3, sticky="w")

        ttk.Label(grid, text="Entry mode").grid(row=5, column=0, sticky="w")
        self.cmb_entry_mode = ttk.Combobox(
            grid,
            textvariable=self.var_entry_mode,
            width=18,
            state="disabled",
            values=["Immediate", "Re-entry 1m", "Re-entry 5m"],
        )
        self.cmb_entry_mode.grid(row=5, column=1, sticky="w", padx=6)

        ttk.Checkbutton(grid, text="Cancel entry on midline", variable=self.var_cancel_on_midline, state="disabled").grid(row=6, column=1, sticky="w", padx=6)
        ttk.Label(grid, text="BB len").grid(row=5, column=2, sticky="w", padx=(20,0))
        self.ent_bb_len = ttk.Entry(grid, textvariable=self.var_bb_len, width=8, state="disabled")
        self.ent_bb_len.grid(row=5, column=3, sticky="w")

        ttk.Label(grid, text="BB mult").grid(row=6, column=2, sticky="w", padx=(20,0))
        self.ent_bb_mult = ttk.Entry(grid, textvariable=self.var_bb_mult, width=8, state="disabled")
        self.ent_bb_mult.grid(row=6, column=3, sticky="w")

        ttk.Checkbutton(grid, text="Use RSI (1m)", variable=self.var_rsi_enabled, state="disabled").grid(row=7, column=0, sticky="w")
        ttk.Label(grid, text="RSI len").grid(row=7, column=2, sticky="w", padx=(20,0))
        self.ent_rsi_len = ttk.Entry(grid, textvariable=self.var_rsi_len, width=8, state="disabled")
        self.ent_rsi_len.grid(row=7, column=3, sticky="w")
        ttk.Label(grid, text="RSI OB").grid(row=8, column=0, sticky="w")
        self.ent_rsi_ob = ttk.Entry(grid, textvariable=self.var_rsi_overbought, width=8, state="disabled")
        self.ent_rsi_ob.grid(row=8, column=1, sticky="w", padx=6)
        ttk.Label(grid, text="RSI OS").grid(row=8, column=2, sticky="w", padx=(20,0))
        self.ent_rsi_os = ttk.Entry(grid, textvariable=self.var_rsi_oversold, width=8, state="disabled")
        self.ent_rsi_os.grid(row=8, column=3, sticky="w")

        ttk.Label(grid, text="Trend filter").grid(row=9, column=0, sticky="w")
        self.cmb_trend_filter = ttk.Combobox(
            grid,
            textvariable=self.var_trend_filter_mode,
            width=22,
            state="disabled",
            values=["5m close vs midline", "Live price vs 5m midline"],
        )
        self.cmb_trend_filter.grid(row=9, column=1, sticky="w", padx=6)
        ttk.Label(grid, text="Retrace ticks").grid(row=9, column=2, sticky="w", padx=(20,0))
        self.ent_retrace = ttk.Entry(grid, textvariable=self.var_retrace_ticks, width=8, state="disabled")
        self.ent_retrace.grid(row=9, column=3, sticky="w")

        ttk.Checkbutton(
            grid,
            text="Trade windows (UTC, always on)",
            variable=self.var_trade_window_enabled,
            state="disabled",
        ).grid(row=10, column=0, sticky="w")
        ttk.Label(grid, text="V1 window").grid(row=10, column=2, sticky="w", padx=(20,0))
        self.ent_trade_windows_v1 = ttk.Entry(grid, textvariable=self.var_trade_windows_v1, width=12)
        self.ent_trade_windows_v1.grid(row=10, column=3, sticky="w")

        ttk.Label(grid, text="V2 window").grid(row=11, column=2, sticky="w", padx=(20,0))
        self.ent_trade_windows_v2 = ttk.Entry(grid, textvariable=self.var_trade_windows_v2, width=12)
        self.ent_trade_windows_v2.grid(row=11, column=3, sticky="w")

        ttk.Checkbutton(
            grid,
            text="Cooldown after SL (always on)",
            variable=self.var_cooldown_enabled,
            state="disabled",
        ).grid(row=12, column=0, sticky="w")
        ttk.Label(grid, text="Cooldown v1 (sec)").grid(row=12, column=2, sticky="w", padx=(20,0))
        self.ent_cooldown_v1_seconds = ttk.Entry(grid, textvariable=self.var_cooldown_v1_seconds, width=8)
        self.ent_cooldown_v1_seconds.grid(row=12, column=3, sticky="w")

        ttk.Label(grid, text="Cooldown v2 (sec)").grid(row=13, column=2, sticky="w", padx=(20,0))
        self.ent_cooldown_v2_seconds = ttk.Entry(grid, textvariable=self.var_cooldown_v2_seconds, width=8)
        self.ent_cooldown_v2_seconds.grid(row=13, column=3, sticky="w")

        ttk.Checkbutton(grid, text="Enable live execution", variable=self.var_exec_enabled, state="disabled").grid(row=14, column=0, sticky="w")
        ttk.Label(grid, text="Order size").grid(row=14, column=2, sticky="w", padx=(20,0))
        self.ent_order_size = ttk.Entry(grid, textvariable=self.var_order_size, width=8)
        self.ent_order_size.grid(row=14, column=3, sticky="w")

        ttk.Label(grid, text="SL mode").grid(row=15, column=0, sticky="w")
        self.cmb_sl_mode = ttk.Combobox(
            grid,
            textvariable=self.var_sl_mode,
            width=18,
            state="readonly",
            values=["Fixed ticks", "Band width", "ATR"],
        )
        self.cmb_sl_mode.grid(row=15, column=1, sticky="w", padx=6)
        ttk.Label(grid, text="SL ticks").grid(row=15, column=2, sticky="w", padx=(20,0))
        self.ent_sl_ticks = ttk.Entry(grid, textvariable=self.var_sl_ticks, width=8)
        self.ent_sl_ticks.grid(row=15, column=3, sticky="w")

        ttk.Label(grid, text="TP targets").grid(row=16, column=0, sticky="w")
        tp_row = ttk.Frame(grid)
        tp_row.grid(row=16, column=1, sticky="w", padx=6, columnspan=3)
        ttk.Checkbutton(tp_row, text="Midline 5m", variable=self.var_tp_mid5).pack(side="left", padx=(0, 6))
        ttk.Checkbutton(tp_row, text="Midline 15m", variable=self.var_tp_mid15).pack(side="left", padx=(0, 6))
        ttk.Checkbutton(tp_row, text="Fixed ticks", variable=self.var_tp_fixed).pack(side="left", padx=(0, 6))
        ttk.Checkbutton(tp_row, text="Band width", variable=self.var_tp_band).pack(side="left", padx=(0, 6))
        ttk.Checkbutton(tp_row, text="ATR", variable=self.var_tp_atr).pack(side="left", padx=(0, 6))

        ttk.Label(grid, text="TP ticks").grid(row=17, column=0, sticky="w")
        self.ent_tp_ticks = ttk.Entry(grid, textvariable=self.var_tp_ticks, width=8)
        self.ent_tp_ticks.grid(row=17, column=1, sticky="w", padx=6)
        ttk.Label(grid, text="SL mult").grid(row=17, column=2, sticky="w", padx=(20,0))
        self.ent_sl_mult = ttk.Entry(grid, textvariable=self.var_sl_mult, width=8)
        self.ent_sl_mult.grid(row=17, column=3, sticky="w")

        ttk.Label(grid, text="TP mult").grid(row=18, column=0, sticky="w")
        self.ent_tp_mult = ttk.Entry(grid, textvariable=self.var_tp_mult, width=8)
        self.ent_tp_mult.grid(row=18, column=1, sticky="w", padx=6)

        btns = ttk.Frame(controls)
        btns.pack(fill="x", pady=(8,0))
        ttk.Button(btns, text="Start Signals", command=self.start_signals).pack(side="left", padx=4)
        ttk.Button(btns, text="Stop Signals", command=self.stop_signals).pack(side="left", padx=4)
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(btns, text="Start Live Feed", command=self.start_live_feed).pack(side="left", padx=4)
        ttk.Button(btns, text="Stop Live Feed", command=self.stop_live_feed).pack(side="left", padx=4)
        ttk.Separator(btns, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(btns, text="Backtest years").pack(side="left")
        ttk.Spinbox(btns, from_=1, to=2, textvariable=self.var_backtest_years, width=4).pack(side="left", padx=6)
        ttk.Button(btns, text="Run Backtest", command=self.run_backtest).pack(side="left", padx=4)
        ttk.Button(btns, text="Export CSV", command=self.export_backtest_csv, state="disabled").pack(side="left", padx=4)
        ttk.Button(btns, text="Auto Tune", command=self.run_auto_tune, state="disabled").pack(side="left", padx=4)
        ttk.Button(btns, text="Test Fetch Bars", command=self.test_fetch_bars, state="disabled").pack(side="left", padx=4)

        log_box = ttk.LabelFrame(top, text="Log", padding=0)
        log_box.pack(fill="both", expand=True, pady=(10,0))
        self.txt_log = tk.Text(log_box, height=18, wrap="none", state="disabled")
        self.txt_log.pack(fill="both", expand=True)

    def _on_contract_selected(self, _evt=None):
        label = self.var_contract.get()
        # label format: "CON... | SYMBOL | name"
        parts = [p.strip() for p in label.split("|")]
        if parts:
            contract_id = parts[0]
            sym = parts[1] if len(parts) > 1 else ""
            self.var_symbol.set(sym)

    # ---------- actions ----------
    def login(self):
        username = self.var_username.get().strip()
        apikey = self.var_apikey.get().strip()
        if not username or not apikey:
            messagebox.showerror("Missing", "Please enter Username and API Key.")
            return

        def worker():
            try:
                self.client.login_with_key(username, apikey)
                self.log("Login success.")
                # Enable actions that require an authenticated session
                self.after(0, lambda: self.btn_load_accounts.configure(state="normal"))
                self.after(0, lambda: self.btn_load_contracts.configure(state="normal"))
                self.after(0, lambda: messagebox.showinfo("Login", "Login success."))
            except Exception as e:
                self.log(f"Login failed: {e}")
                self.after(0, lambda err=str(e): messagebox.showerror("Login failed", err))

        threading.Thread(target=worker, daemon=True).start()

    def load_accounts(self):
        if not getattr(self.client, "token", None):
            messagebox.showerror("Not logged in", "Please Login first.")
            return
        def worker():
            try:
                data = self.client.account_search(only_active_accounts=True)
                self.accounts = data.get("accounts") or []
                items = []
                for a in self.accounts:
                    items.append(f"{a.get('id')} | {a.get('name')}")
                self.after(0, lambda: self._set_accounts(items))
                self.log(f"Loaded {len(items)} accounts.")
            except Exception as e:
                self.log(f"Load accounts failed: {e}")
                self.after(0, lambda err=str(e): messagebox.showerror("Load accounts failed", err))
        threading.Thread(target=worker, daemon=True).start()

    def _set_accounts(self, items):
        self.cmb_account["values"] = items
        if items and not self.var_account.get():
            self.var_account.set(items[0])

    def load_contracts(self):
        if not getattr(self.client, "token", None):
            messagebox.showerror("Not logged in", "Please Login first.")
            return
        live = bool(self.var_live.get())
        def worker():
            try:
                data = self.client.contract_available(live=live)
                contracts = data.get("contracts") or []
                if not contracts:
                    # fallback to a broad search if available
                    data2 = self.client.contract_search(query="GC", live=live)
                    contracts = data2.get("contracts") or []
                self.contracts = contracts
                items = []
                for c in contracts:
                    cid = c.get("contractId") or c.get("id") or ""
                    sym = c.get("symbolId") or c.get("symbol") or ""
                    name = c.get("name") or c.get("description") or ""
                    if cid:
                        items.append(f"{cid} | {sym} | {name}".strip())
                self.after(0, lambda: self._set_contracts(items))
                self.log(f"Loaded {len(items)} contracts.")
            except Exception as e:
                self.log(f"Load contracts failed: {e}")
                self.after(0, lambda err=str(e): messagebox.showerror("Load contracts failed", err))
        threading.Thread(target=worker, daemon=True).start()

    def _set_contracts(self, items):
        def _pick_micro_gold(opts):
            for item in opts:
                parts = [p.strip() for p in item.split("|")]
                sym = parts[1] if len(parts) > 1 else ""
                name = parts[2] if len(parts) > 2 else ""
                if "MGC" in sym.upper():
                    return item
                if "MGC" in name.upper():
                    return item
                if "MICRO GOLD" in name.upper() or "MICROGOLD" in name.upper():
                    return item
            return opts[0] if opts else ""

        picked = _pick_micro_gold(items)
        if picked:
            self.cmb_contract["values"] = [picked]
            self.var_contract.set(picked)
            self._on_contract_selected()
            if "MGC" not in picked:
                self.log("Auto contract: micro gold not found; using first available contract.")
        else:
            self.cmb_contract["values"] = []

    def _selected_contract_id(self) -> str:
        label = self.var_contract.get().strip()
        if not label:
            raise ValueError("No contract selected")
        return label.split("|")[0].strip()

    def _selected_account_id(self) -> int:
        label = self.var_account.get().strip()
        if not label:
            raise ValueError("No account selected")
        return int(label.split("|")[0].strip())

    def _selected_symbol_id(self) -> str:
        sym = self.var_symbol.get().strip()
        if not sym:
            # try parse from contract label
            label = self.var_contract.get().strip()
            parts = [p.strip() for p in label.split("|")]
            sym = parts[1] if len(parts) > 1 else ""
        if not sym:
            raise ValueError("No symbolId available for selected contract")
        return sym

    def _update_trade_display(self, trade: TradeState | None):
        if not trade:
            self.after(0, lambda: self.var_state.set("FLAT"))
            self.after(0, lambda: self.var_entry_sl_tp.set("NA"))
            return
        status = "PENDING" if trade.status == "pending_entry" else "ACTIVE"
        self.after(0, lambda: self.var_state.set(f"{trade.direction} {status}"))
        if trade.entry_price and trade.sl_price and trade.tp_prices:
            tp_preview = ",".join(f"{p:.2f}" for p in trade.tp_prices[:3])
            more = "" if len(trade.tp_prices) <= 3 else "..."
            line = f"E={trade.entry_price:.2f} SL={trade.sl_price:.2f} TP={tp_preview}{more} +BBop(live)"
        elif trade.entry_price:
            line = f"E={trade.entry_price:.2f} SL=... TP=..."
        else:
            if trade.entry_limit is not None:
                line = f"E<= {trade.entry_limit:.2f} SL=... TP=..."
            else:
                line = "E=... SL=... TP=..."
        self.after(0, lambda v=line: self.var_entry_sl_tp.set(v))

    def _set_last_price_from_loop(self, price: float):
        self._last_price_value = float(price)
        self.after(0, lambda v=f"{price:.2f}": self.var_last_price.set(v))

    def _update_daily_pnl_from_tracker(self, realized_ticks: float, unrealized_ticks: float):
        total = float(realized_ticks) + float(unrealized_ticks)
        msg = f"Daily ticks: {total:.1f} (R: {realized_ticks:.1f} U: {unrealized_ticks:.1f})"
        self.after(0, lambda v=msg: self.var_daily_pnl.set(v))

    def _is_daily_locked(self) -> bool:
        return self._pnl_tracker.is_locked()

    def _get_trading_settings_base(self) -> dict:
        return {
            "live": bool(self.var_live.get()),
            "offset": int(self.var_offset.get()),
            "sl_mode": self.var_sl_mode.get(),
            "tp_mid5": bool(self.var_tp_mid5.get()),
            "tp_mid15": bool(self.var_tp_mid15.get()),
            "tp_fixed": bool(self.var_tp_fixed.get()),
            "tp_band": bool(self.var_tp_band.get()),
            "tp_atr": bool(self.var_tp_atr.get()),
            "sl_ticks": int(self.var_sl_ticks.get()),
            "tp_ticks": int(self.var_tp_ticks.get()),
            "sl_mult": float(self.var_sl_mult.get()),
            "tp_mult": float(self.var_tp_mult.get()),
            "tick_size": float(self.var_tick_size.get()),
            "tick_value": float(self.var_tick_value.get()),
            "entry_offset_ticks": int(self.var_entry_offset_ticks.get()),
            "entry_timeout_min": int(self.var_entry_timeout_min.get()),
            "entry_mode": self.var_entry_mode.get(),
            "cancel_on_midline": bool(self.var_cancel_on_midline.get()),
            "bb_len": int(self.var_bb_len.get()),
            "bb_mult": float(self.var_bb_mult.get()),
            "rsi_enabled": bool(self.var_rsi_enabled.get()),
            "rsi_len": int(self.var_rsi_len.get()),
            "rsi_overbought": float(self.var_rsi_overbought.get()),
            "rsi_oversold": float(self.var_rsi_oversold.get()),
            "retrace_ticks": int(self.var_retrace_ticks.get()),
            "trend_filter_mode": str(self.var_trend_filter_mode.get()),
            "trade_window_enabled": bool(self.var_trade_window_enabled.get()),
            "cooldown_enabled": bool(self.var_cooldown_enabled.get()),
            "exec_enabled": bool(self.var_exec_enabled.get()),
            "order_size": int(self.var_order_size.get()),
        }

    def _get_trading_settings_v1(self) -> dict:
        settings = self._get_trading_settings_base()
        settings["cooldown_enabled"] = True
        settings["cooldown_seconds"] = int(self.var_cooldown_v1_seconds.get())
        settings["trade_windows"] = str(self.var_trade_windows_v1.get())
        return settings

    def _get_trading_settings_v2(self) -> dict:
        settings = self._get_trading_settings_base()
        settings["cooldown_enabled"] = True
        settings["cooldown_seconds"] = int(self.var_cooldown_v2_seconds.get())
        settings["trade_windows"] = str(self.var_trade_windows_v2.get())
        return settings

    def start_signals(self):
        try:
            self._switcher.start_signals()
        except Exception as e:
            messagebox.showerror("Start failed", str(e))

    def start_user_hub(self):
        self._pnl_tracker.start()

    def stop_signals(self):
        self._switcher.stop_signals()

    def start_live_feed(self):
        self._switcher.start_live_feed()

    def stop_live_feed(self):
        self._switcher.stop_live_feed()

    def run_backtest(self):
        try:
            contract_id = self._selected_contract_id()
        except Exception as e:
            messagebox.showerror("Backtest failed", str(e))
            return
        years = int(self.var_backtest_years.get())
        if years not in (1, 2):
            messagebox.showerror("Backtest failed", "Years must be 1 or 2.")
            return
        if not self.client.token:
            messagebox.showerror("Backtest failed", "Login first (token required).")
            return

        def worker():
            try:
                end = datetime.now(timezone.utc)
                start = end - timedelta(days=365 * years)
                cfg = BacktestConfig(
                    contract_id=contract_id,
                    live=bool(self.var_live.get()),
                    start_time=start,
                    end_time=end,
                    sl_mode=self.var_sl_mode.get(),
                    tp_mid5=bool(self.var_tp_mid5.get()),
                    tp_mid15=bool(self.var_tp_mid15.get()),
                    tp_fixed=bool(self.var_tp_fixed.get()),
                    tp_band=bool(self.var_tp_band.get()),
                    tp_atr=bool(self.var_tp_atr.get()),
                    sl_ticks=int(self.var_sl_ticks.get()),
                    tp_ticks=int(self.var_tp_ticks.get()),
                    sl_mult=float(self.var_sl_mult.get()),
                    tp_mult=float(self.var_tp_mult.get()),
                    tick_size=float(self.var_tick_size.get()),
                    entry_offset_ticks=int(self.var_entry_offset_ticks.get()),
                    tick_value=float(self.var_tick_value.get()),
                    lot_size=int(self.var_backtest_lot_size.get()),
                    entry_timeout_min=int(self.var_entry_timeout_min.get()),
                    entry_mode=self.var_entry_mode.get(),
                    cancel_on_midline_before_fill=bool(self.var_cancel_on_midline.get()),
                    bb_len=int(self.var_bb_len.get()),
                    bb_mult=float(self.var_bb_mult.get()),
                    rsi_enabled=bool(self.var_rsi_enabled.get()),
                    rsi_len=int(self.var_rsi_len.get()),
                    rsi_overbought=float(self.var_rsi_overbought.get()),
                    rsi_oversold=float(self.var_rsi_oversold.get()),
                    trend_filter_mode=str(self.var_trend_filter_mode.get()),
                    retrace_ticks=int(self.var_retrace_ticks.get()),
                    trade_window_enabled=bool(self.var_trade_window_enabled.get()),
                    trade_windows_v1=str(self.var_trade_windows_v1.get()),
                    trade_windows_v2=str(self.var_trade_windows_v2.get()),
                    cooldown_enabled=bool(self.var_cooldown_enabled.get()),
                    cooldown_seconds_v1=int(self.var_cooldown_v1_seconds.get()),
                    cooldown_seconds_v2=int(self.var_cooldown_v2_seconds.get()),
                    daily_profit_enabled=True,
                    daily_profit_ticks=float(DAILY_PROFIT_TICKS),
                    daily_reset_hour=int(DAILY_RESET_LOCAL_HOUR),
                    daily_reset_minute=int(DAILY_RESET_LOCAL_MINUTE),
                )
                self.log(f"Backtest started ({years}y). This may take a while...")
                res = run_backtest(self.client, cfg)
                self._last_backtest = res
                self.log(
                    f"Backtest done. trades={len(res.trades)} wins={res.wins} losses={res.losses} "
                    f"winrate={res.win_rate:.1f}% total_ticks={res.total_pnl_ticks:.1f} "
                    f"avg_ticks={res.avg_pnl_ticks:.2f} total_price={res.total_pnl_price:.2f} "
                    f"total_usd={res.total_pnl_usd:.2f} "
                    f"max_consec_loss_usd={res.max_consecutive_loss_usd:.2f} "
                    f"skipped={res.skipped}"
                )
                if res.max_consecutive_loss_usd > 4500:
                    self.log("Loss limit breached: max_consec_loss_usd > 4500.")
                    if res.max_consecutive_loss_start and res.max_consecutive_loss_end:
                        self.log(
                            f"Max loss streak: {res.max_consecutive_loss_start.isoformat()} → "
                            f"{res.max_consecutive_loss_end.isoformat()} "
                            f"equity {res.equity_at_max_consec_start:.2f} → {res.equity_at_max_consec_end:.2f}"
                        )
                if res.min_equity_time is not None:
                    self.log(f"Lowest equity: {res.min_equity:.2f} at {res.min_equity_time.isoformat()}")
                if res.trailing_limit_breached:
                    when = res.trailing_limit_breach_time.isoformat() if res.trailing_limit_breach_time else "unknown"
                    self.log(f"Trailing loss limit breached at {when}. Min margin={res.trailing_limit_min_margin:.2f}")
                else:
                    self.log(f"Trailing loss limit OK. Min margin={res.trailing_limit_min_margin:.2f}")
                if res.data_start and res.data_end:
                    self.log(f"Data coverage: {res.data_start.isoformat()} → {res.data_end.isoformat()}")
                self.log("Note: ticks = price_move / tick_size.")
                for i, t in enumerate(res.trades, 1):
                    tp_list = ",".join(f"{p:.2f}" for p in t.tp_prices)
                    tp_hit = f"{t.tp_hit_price:.2f}" if t.tp_hit_price is not None else "NA"
                    self.log(
                        f"Trade {i}: {t.direction} entry={t.entry_price:.2f} "
                        f"exit={t.exit_price:.2f} reason={t.exit_reason} "
                        f"pnl_price={t.pnl_price:.2f} pnl_ticks={t.pnl_ticks:.2f} pnl_usd={t.pnl_usd:.2f} "
                        f"SL={t.sl_price:.2f} TP_hit={tp_hit} TP_list={tp_list} "
                        f"entry_time={t.entry_time.isoformat()} exit_time={t.exit_time.isoformat()}"
                    )
                if res.skipped_signals:
                    self.log(f"Skipped signals: {len(res.skipped_signals)}")
                    for i, s in enumerate(res.skipped_signals, 1):
                        cancels = ",".join(f"{v:.2f}" for v in s.cancel_levels) if s.cancel_levels else "NA"
                        limit_str = f"{s.entry_limit:.2f}" if s.entry_limit is not None else "NA"
                        self.log(
                            f"Skipped {i}: {s.direction} reason={s.reason} trigger_close={s.trigger_close:.2f} "
                            f"entry_limit={limit_str} cancel_levels={cancels} "
                            f"trigger_time={s.trigger_time.isoformat()}"
                        )
            except Exception as e:
                self.log(f"Backtest error: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def run_auto_tune(self):
        try:
            contract_id = self._selected_contract_id()
        except Exception as e:
            messagebox.showerror("Auto tune failed", str(e))
            return
        years = int(self.var_backtest_years.get())
        if years not in (1, 2):
            messagebox.showerror("Auto tune failed", "Years must be 1 or 2.")
            return
        if not self.client.token:
            messagebox.showerror("Auto tune failed", "Login first (token required).")
            return

        def worker():
            try:
                end = datetime.now(timezone.utc)
                start = end - timedelta(days=365 * years)
                base_cfg = BacktestConfig(
                    contract_id=contract_id,
                    live=bool(self.var_live.get()),
                    start_time=start,
                    end_time=end,
                    sl_mode=self.var_sl_mode.get(),
                    tp_mid5=bool(self.var_tp_mid5.get()),
                    tp_mid15=bool(self.var_tp_mid15.get()),
                    tp_fixed=bool(self.var_tp_fixed.get()),
                    tp_band=bool(self.var_tp_band.get()),
                    tp_atr=bool(self.var_tp_atr.get()),
                    sl_ticks=int(self.var_sl_ticks.get()),
                    tp_ticks=int(self.var_tp_ticks.get()),
                    sl_mult=float(self.var_sl_mult.get()),
                    tp_mult=float(self.var_tp_mult.get()),
                    tick_size=float(self.var_tick_size.get()),
                    entry_offset_ticks=int(self.var_entry_offset_ticks.get()),
                    tick_value=float(self.var_tick_value.get()),
                    lot_size=int(self.var_backtest_lot_size.get()),
                    entry_timeout_min=int(self.var_entry_timeout_min.get()),
                    entry_mode=self.var_entry_mode.get(),
                    bb_len=int(self.var_bb_len.get()),
                    bb_mult=float(self.var_bb_mult.get()),
                    rsi_enabled=bool(self.var_rsi_enabled.get()),
                    rsi_len=int(self.var_rsi_len.get()),
                    rsi_overbought=float(self.var_rsi_overbought.get()),
                    rsi_oversold=float(self.var_rsi_oversold.get()),
                    trend_filter_mode=str(self.var_trend_filter_mode.get()),
                    retrace_ticks=int(self.var_retrace_ticks.get()),
                    cooldown_enabled=bool(self.var_cooldown_enabled.get()),
                )
                self.log("Auto tune started. This may take a while...")
                workers = min(3, os.cpu_count() or 1)
                last_percent = {"value": -1}

                def _progress(done: int, total: int):
                    if total <= 0:
                        return
                    percent = int((done / total) * 100)
                    if percent >= last_percent["value"] + 5 or done == total:
                        last_percent["value"] = percent
                        self.log(f"Auto tune progress: {done}/{total} ({percent}%)")

                results, info = run_backtest_grid(
                    self.client,
                    base_cfg,
                    parallel=True,
                    max_workers=workers,
                    progress_callback=_progress,
                )
                if not results:
                    self.log("Auto tune: no data available.")
                    return
                if info.get("grid_thinned"):
                    self.log("Auto tune: grid thinned to ~2000 combos.")
                if info.get("parallel"):
                    self.log(f"Auto tune: parallel enabled (workers={info.get('workers')})")
                results_sorted = sorted(results, key=lambda r: (r.total_pnl_usd, -r.max_consecutive_loss_usd, r.win_rate), reverse=True)
                top = results_sorted[:10]
                if top and top[0].data_start and top[0].data_end:
                    self.log(f"Auto tune data coverage: {top[0].data_start.isoformat()} → {top[0].data_end.isoformat()}")
                self.log(f"Auto tune done. combinations={len(results_sorted)}")
                for i, r in enumerate(top, 1):
                    cfg = r.cfg
                    self.log(
                        f"Top {i}: pnl_usd={r.total_pnl_usd:.2f} winrate={r.win_rate:.1f}% "
                        f"trades={r.trades} skipped={r.skipped} bb={cfg.bb_len}/{cfg.bb_mult} "
                        f"entry_mode={cfg.entry_mode} trend={cfg.trend_filter_mode} rsi={'on' if cfg.rsi_enabled else 'off'} "
                        f"offset={cfg.entry_offset_ticks} timeout={cfg.entry_timeout_min} "
                        f"max_consec_loss_usd={r.max_consecutive_loss_usd:.2f}"
                    )
                within_limit = [r for r in results_sorted if r.max_consecutive_loss_usd <= 4500]
                if within_limit:
                    self.log(f"Auto tune within $4500 loss limit: {len(within_limit)} combos")
                    for i, r in enumerate(within_limit[:10], 1):
                        cfg = r.cfg
                        self.log(
                            f"Top (limit) {i}: pnl_usd={r.total_pnl_usd:.2f} winrate={r.win_rate:.1f}% "
                            f"trades={r.trades} skipped={r.skipped} bb={cfg.bb_len}/{cfg.bb_mult} "
                            f"entry_mode={cfg.entry_mode} trend={cfg.trend_filter_mode} rsi={'on' if cfg.rsi_enabled else 'off'} "
                            f"offset={cfg.entry_offset_ticks} timeout={cfg.entry_timeout_min} "
                            f"max_consec_loss_usd={r.max_consecutive_loss_usd:.2f}"
                        )
                else:
                    self.log("Auto tune: no combos within $4500 max consecutive loss.")

                filename = f"autotune_results_{end.strftime('%Y%m%d_%H%M%S')}.csv"
                path = os.path.join(os.getcwd(), filename)
                import csv
                with open(path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(
                        [
                            "bb_len",
                            "bb_mult",
                            "entry_mode",
                            "trend_filter_mode",
                            "rsi_enabled",
                            "cooldown_enabled",
                            "entry_offset_ticks",
                            "entry_timeout_min",
                            "trades",
                            "wins",
                            "losses",
                            "win_rate",
                            "total_pnl_ticks",
                            "total_pnl_usd",
                            "skipped",
                            "max_consec_loss_usd",
                            "within_loss_limit",
                            "data_start",
                            "data_end",
                        ]
                    )
                    for r in results_sorted:
                        cfg = r.cfg
                        writer.writerow(
                            [
                                cfg.bb_len,
                                cfg.bb_mult,
                                cfg.entry_mode,
                            cfg.trend_filter_mode,
                                cfg.rsi_enabled,
                            cfg.cooldown_enabled,
                                cfg.entry_offset_ticks,
                                cfg.entry_timeout_min,
                                r.trades,
                                r.wins,
                                r.losses,
                                f"{r.win_rate:.2f}",
                                f"{r.total_pnl_ticks:.2f}",
                                f"{r.total_pnl_usd:.2f}",
                                r.skipped,
                                f"{r.max_consecutive_loss_usd:.2f}",
                                "yes" if r.max_consecutive_loss_usd <= 4500 else "no",
                                r.data_start.isoformat() if r.data_start else "",
                                r.data_end.isoformat() if r.data_end else "",
                            ]
                        )
                self.log(f"Auto tune CSV saved: {path}")
            except Exception as e:
                self.log(f"Auto tune error: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def export_backtest_csv(self):
        if not self._last_backtest or not self._last_backtest.trades:
            messagebox.showerror("Export failed", "Run a backtest first.")
            return
        path = filedialog.asksaveasfilename(
            title="Export Backtest CSV",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if not path:
            return
        try:
            import csv

            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "trade",
                        "direction",
                        "trigger_time",
                        "trigger_close",
                        "confirm_time",
                        "confirm_close",
                        "entry_limit",
                        "entry_time",
                        "entry_price",
                        "exit_time",
                        "exit_price",
                        "exit_reason",
                        "pnl_price",
                        "pnl_ticks",
                        "pnl_usd",
                        "sl_price",
                        "tp_hit_price",
                        "tp_prices",
                        "status",
                        "skip_reason",
                        "cancel_levels",
                    ]
                )
                for i, t in enumerate(self._last_backtest.trades, 1):
                    writer.writerow(
                        [
                            i,
                            t.direction,
                            t.trigger_time.isoformat(),
                            f"{t.trigger_close:.2f}",
                            t.confirm_time.isoformat() if t.confirm_time else "",
                            f"{t.confirm_close:.2f}" if t.confirm_close is not None else "",
                            f"{t.entry_limit:.2f}",
                            t.entry_time.isoformat(),
                            f"{t.entry_price:.2f}",
                            t.exit_time.isoformat(),
                            f"{t.exit_price:.2f}",
                            t.exit_reason,
                            f"{t.pnl_price:.2f}",
                            f"{t.pnl_ticks:.2f}",
                            f"{t.pnl_usd:.2f}",
                            f"{t.sl_price:.2f}",
                            f"{t.tp_hit_price:.2f}" if t.tp_hit_price is not None else "",
                            ";".join(f"{p:.2f}" for p in t.tp_prices),
                            "trade",
                            "",
                            "",
                        ]
                    )
                for i, s in enumerate(self._last_backtest.skipped_signals, 1):
                    writer.writerow(
                        [
                            f"skip_{i}",
                            s.direction,
                            s.trigger_time.isoformat(),
                            f"{s.trigger_close:.2f}",
                            "",
                            "",
                            f"{s.entry_limit:.2f}" if s.entry_limit is not None else "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "",
                            "skipped",
                            s.reason,
                            ";".join(f"{v:.2f}" for v in s.cancel_levels),
                        ]
                    )
            self.log(f"Backtest CSV exported: {path}")
        except Exception as e:
            self.log(f"CSV export error: {e}")

    def _build_calendar_tab(self):
        top = ttk.Frame(self.tab_cal, padding=12)
        top.pack(fill="both", expand=True)

        controls = ttk.LabelFrame(top, text="News Calendar", padding=10)
        controls.pack(fill="x")

        row = ttk.Frame(controls)
        row.pack(fill="x")
        ttk.Checkbutton(row, text="Enable calendar filter", variable=self.var_calendar_enabled).pack(side="left")

        form = ttk.Frame(controls)
        form.pack(fill="x", pady=(8, 0))
        ttk.Label(form, text="Date (YYYY-MM-DD)").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.var_calendar_date, width=16).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(form, text="Time (HH:MM UTC)").grid(row=0, column=2, sticky="w", padx=(20, 0))
        ttk.Entry(form, textvariable=self.var_calendar_time, width=10).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Label(form, text="Name").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(form, textvariable=self.var_calendar_name, width=30).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0), columnspan=3)
        ttk.Label(form, text="Pre min").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(form, textvariable=self.var_calendar_pre_min, width=8).grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(form, text="Post min").grid(row=2, column=2, sticky="w", padx=(20, 0), pady=(6, 0))
        ttk.Entry(form, textvariable=self.var_calendar_post_min, width=8).grid(row=2, column=3, sticky="w", padx=6, pady=(6, 0))

        btns = ttk.Frame(controls)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Add", command=self._add_calendar_event).pack(side="left", padx=4)
        ttk.Button(btns, text="Remove Selected", command=self._remove_calendar_event).pack(side="left", padx=4)

        list_box = ttk.LabelFrame(top, text="Events (UTC)", padding=6)
        list_box.pack(fill="both", expand=True, pady=(10, 0))
        self.lst_calendar = tk.Listbox(list_box, height=12)
        self.lst_calendar.pack(fill="both", expand=True)
        self._refresh_calendar_list()

    def _refresh_calendar_list(self):
        if not hasattr(self, "lst_calendar"):
            return
        self.lst_calendar.delete(0, "end")
        for e in self._calendar_events:
            label = f"{e.date} {e.time} | {e.name} (pre {e.pre_min} / post {e.post_min} min)"
            self.lst_calendar.insert("end", label)

    def _add_calendar_event(self):
        date = self.var_calendar_date.get().strip()
        time_str = self.var_calendar_time.get().strip()
        name = self.var_calendar_name.get().strip()
        if not date or not time_str:
            messagebox.showerror("Calendar", "Date and time are required.")
            return
        try:
            pre_min = int(self.var_calendar_pre_min.get())
            post_min = int(self.var_calendar_post_min.get())
        except Exception:
            messagebox.showerror("Calendar", "Pre/Post minutes must be integers.")
            return
        event = CalendarEvent(date=date, time=time_str, name=name, pre_min=pre_min, post_min=post_min)
        self._calendar_events.append(event)
        save_events(self._calendar_path, self._calendar_events)
        self._refresh_calendar_list()

    def _remove_calendar_event(self):
        if not hasattr(self, "lst_calendar"):
            return
        idxs = list(self.lst_calendar.curselection())
        if not idxs:
            return
        for idx in reversed(idxs):
            try:
                self._calendar_events.pop(idx)
            except Exception:
                continue
        save_events(self._calendar_path, self._calendar_events)
        self._refresh_calendar_list()

    def _calendar_block_reason(self) -> str | None:
        if not self.var_calendar_enabled.get():
            return None
        if not self._calendar_events:
            return None
        return is_blocked(datetime.now(timezone.utc), self._calendar_events)

    def test_fetch_bars(self):
        try:
            contract_id = self._selected_contract_id()
        except Exception as e:
            messagebox.showerror("Test fetch failed", str(e))
            return
        if not self.client.token:
            messagebox.showerror("Test fetch failed", "Login first (token required).")
            return
        live = bool(self.var_live.get())

        def worker():
            try:
                for tf in ("1m", "5m", "15m"):
                    for partial in (False, True):
                        data = self.client.retrieve_bars(
                            contract_id,
                            tf,
                            limit=200,
                            live=live,
                            include_partial_bar=partial,
                        )
                        bars = data.get("bars") or data.get("candles") or data.get("data") or []
                        self.log(
                            f"Test fetch {tf} partial={partial} live={live} "
                            f"bars={len(bars)} contractId={contract_id}"
                        )
                # Explicit range test (last 7 days) to check historical availability
                end_dt = datetime.now(timezone.utc)
                start_dt = end_dt - timedelta(days=7)
                data = self.client.retrieve_bars(
                    contract_id,
                    "15m",
                    limit=20000,
                    live=live,
                    start_time=start_dt.isoformat(),
                    end_time=end_dt.isoformat(),
                    include_partial_bar=False,
                )
                bars = data.get("bars") or data.get("candles") or data.get("data") or []
                self.log(
                    f"Test fetch 15m range live={live} "
                    f"bars={len(bars)} start={start_dt.isoformat()} end={end_dt.isoformat()}"
                )
                self.log("Test fetch complete.")
            except Exception as e:
                self.log(f"Test fetch error: {e}")

        threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
    Dashboard().mainloop()

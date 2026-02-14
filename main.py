from __future__ import annotations

"""
CLI entrypoint for TopstepX MF v5 (Raspberry Pi headless).

- Uses TheBigCheese Gold Algo (v1_5 logic).
- Enforces trade window 00:00–05:30 UTC via settings passed to TradingLoopV1.
- No Tkinter / GUI; runs as a long‑lived process on a minimal OS.

Edit the CONFIG dict below to match your credentials and risk settings,
or override with environment variables where noted.
"""

import json
import os
import sys
import signal
import time
import threading
from dataclasses import dataclass
from typing import Dict, Any

from topstepx.api_client import TopstepXClient, ApiError
from topstepx.trading_loop_v1 import TradingLoopV1, TradeState
from topstepx.user_hub import UserHubClient, UserPosition


@dataclass
class RuntimeConfig:
    username: str
    api_key: str
    live: bool
    order_size: int
    exec_enabled: bool
    cooldown_seconds: int
    trade_windows: str
    preferred_account_id: int | None = None


CONFIG = RuntimeConfig(
    # Username/API key are loaded or prompted and then cached, see _load_or_prompt_credentials.
    username="",
    api_key="",
    live=True,
    # Live/combine default lot size 3; override via TOPSTEPX_ORDER_SIZE if needed.
    order_size=int(os.environ.get("TOPSTEPX_ORDER_SIZE", "3")),
    exec_enabled=True,
    cooldown_seconds=55,
    # v5: trade window 00:00–05:30 UTC only.
    trade_windows="00:00-16:00",
    # Optional: force a specific accountId via env
    preferred_account_id=int(os.environ["TOPSTEPX_ACCOUNT_ID"]) if os.environ.get("TOPSTEPX_ACCOUNT_ID") else None,
)


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    print(f"{ts} | {msg}", flush=True)


def _credentials_path() -> str:
    # Store alongside this script so it works the same on Pi and desktop.
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "credentials.json")


def _load_or_prompt_credentials() -> tuple[str, str]:
    """
    Load username/API key from credentials.json, env vars, or prompt once and cache.
    """
    path = _credentials_path()

    # 1) Existing credentials file.
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            u = (data.get("username") or "").strip()
            k = (data.get("api_key") or "").strip()
            if u and k:
                _log(f"Loaded credentials from {path}")
                return u, k
    except Exception:
        # Fall back to env/prompt if file is unreadable.
        pass

    # 2) Environment variables.
    env_u = (os.environ.get("TOPSTEPX_USERNAME") or "").strip()
    env_k = (os.environ.get("TOPSTEPX_API_KEY") or "").strip()
    if env_u and env_k:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"username": env_u, "api_key": env_k}, f)
            _log(f"Saved credentials to {path} from environment variables.")
        except Exception:
            _log("Warning: failed to save credentials file, continuing with env vars only.")
        return env_u, env_k

    # 3) Prompt once on CLI.
    print("Enter TopstepX username: ", end="", flush=True)
    u = input().strip()
    print("Enter TopstepX API key: ", end="", flush=True)
    k = input().strip()
    if not u or not k:
        _log("ERROR: Username or API key empty.")
        sys.exit(1)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"username": u, "api_key": k}, f)
        _log(f"Saved credentials to {path}")
    except Exception:
        _log("Warning: failed to save credentials file, continuing with in-memory credentials only.")
    return u, k


def _discover_account_and_contract(client: TopstepXClient) -> tuple[int, str, str]:
    """
    Pick account (list + prompt or TOPSTEPX_ACCOUNT_ID) and first MGC contract.
    Returns (account_id, contract_id, symbol).
    """
    data_acc = client.account_search(only_active_accounts=True)
    accounts = data_acc.get("accounts") or data_acc.get("items") or []
    if not accounts:
        raise RuntimeError("No active accounts returned from /api/Account/search")

    # 1) If a specific accountId is configured, honour it without prompting.
    if CONFIG.preferred_account_id is not None:
        for a in accounts:
            aid = a.get("accountId") or a.get("id")
            try:
                if aid is not None and int(aid) == int(CONFIG.preferred_account_id):
                    account_id = int(aid)
                    acc = a
                    _log(f"Using preferred accountId={account_id} from TOPSTEPX_ACCOUNT_ID.")
                    break
            except Exception:
                continue
        else:
            _log(
                f"Preferred accountId={CONFIG.preferred_account_id} not found in account list; "
                f"falling back to interactive selection."
            )
            acc = None
    else:
        acc = None

    # 2) If no preferred account (or it wasn't found), show a numbered list and prompt.
    if acc is None:
        print("Available accounts:")
        for idx, a in enumerate(accounts, start=1):
            aid = a.get("accountId") or a.get("id")
            name = a.get("name") or a.get("displayName") or ""
            acct_type = a.get("accountType") or ""
            print(f"{idx}) {aid} | {name} | {acct_type}")
        while True:
            choice = input(f"Select account number [1-{len(accounts)}] (blank = 1): ").strip()
            if not choice:
                sel = 1
            else:
                try:
                    sel = int(choice)
                except ValueError:
                    print("Please enter a valid number.")
                    continue
            if 1 <= sel <= len(accounts):
                acc = accounts[sel - 1]
                break
            print("Selection out of range.")

    account_id = int(acc.get("accountId") or acc.get("id"))

    # Try available contracts for the configured live flag first.
    contracts: list[dict] = []
    tried_msgs: list[str] = []
    try:
        data_ct = client.contract_available(live=CONFIG.live)
        contracts = data_ct.get("contracts") or data_ct.get("items") or []
        tried_msgs.append(f"/api/Contract/available live={CONFIG.live}")
    except Exception as e:
        tried_msgs.append(f"/api/Contract/available live={CONFIG.live} error={e}")

    # Fallback: try the opposite live flag if nothing came back.
    if not contracts:
        alt_live = not CONFIG.live
        try:
            data_ct = client.contract_available(live=alt_live)
            contracts = data_ct.get("contracts") or data_ct.get("items") or []
            tried_msgs.append(f"/api/Contract/available live={alt_live}")
        except Exception as e:
            tried_msgs.append(f"/api/Contract/available live={alt_live} error={e}")

    # Fallback 2: try a generic contract search for MGC.
    if not contracts:
        try:
            data_ct = client.contract_search("MGC", live=CONFIG.live)
            contracts = data_ct.get("contracts") or data_ct.get("items") or []
            tried_msgs.append(f"/api/Contract/search 'MGC' live={CONFIG.live}")
        except Exception as e:
            tried_msgs.append(f"/api/Contract/search 'MGC' live={CONFIG.live} error={e}")

    if not contracts:
        raise RuntimeError(
            "No contracts discovered from TopstepX. Tried: " + "; ".join(tried_msgs)
        )

    chosen = None
    for c in contracts:
        name = (c.get("name") or "").upper()
        sym = (c.get("symbol") or "").upper()
        if "MGC" in name or "MGC" in sym:
            chosen = c
            break
    if not chosen:
        chosen = contracts[0]

    contract_id = str(chosen.get("contractId") or chosen.get("id"))
    symbol = str(chosen.get("symbol") or chosen.get("name") or contract_id)
    _log(f"Using accountId={account_id}, contractId={contract_id}, symbol={symbol}")
    return account_id, contract_id, symbol


def _build_settings() -> Dict[str, Any]:
    """
    Build settings dict compatible with TradingLoopV1._get_trading_settings_v1.
    Most values are kept at sane defaults; adjust as needed.
    """
    return {
        "live": CONFIG.live,
        "offset": 0,
        "sl_mode": "Fixed ticks",
        "tp_mid5": True,
        "tp_mid15": False,
        "tp_fixed": True,
        "tp_band": True,
        "tp_atr": False,
        # Match v5.1 practice model: 50-tick SL, 100-tick TP.
        "sl_ticks": 50,
        "tp_ticks": 100,
        "sl_mult": 1.0,
        "tp_mult": 1.0,
        "tick_size": 0.1,
        "tick_value": 1.0,
        "entry_offset_ticks": 0,
        "entry_timeout_min": 2,
        "entry_mode": "Immediate",
        "cancel_on_midline": True,
        # Bollinger Bands: 8-period, 2.0x multiplier.
        "bb_len": 8,
        "bb_mult": 2.0,
        "rsi_enabled": False,
        "rsi_len": 14,
        "rsi_overbought": 70.0,
        "rsi_oversold": 30.0,
        "retrace_ticks": 0,
        "trend_filter_mode": "disabled",
        "trade_window_enabled": True,
        "trade_windows": CONFIG.trade_windows,
        "cooldown_enabled": True,
        "cooldown_seconds": CONFIG.cooldown_seconds,
        "exec_enabled": CONFIG.exec_enabled,
        "order_size": CONFIG.order_size,
    }


def main() -> None:
    # Ensure we have credentials, loading/saving as needed.
    username, api_key = _load_or_prompt_credentials()
    CONFIG.username = username
    CONFIG.api_key = api_key

    client = TopstepXClient()
    try:
        _log("Logging in...")
        client.login_with_key(CONFIG.username, CONFIG.api_key)
        _log("Login success.")
    except ApiError as e:
        _log(f"Login failed: {e}")
        sys.exit(1)

    try:
        account_id, contract_id, symbol = _discover_account_and_contract(client)
    except Exception as e:
        _log(f"Discovery failed: {e}")
        sys.exit(1)

    settings = _build_settings()
    last_price_holder = {"val": None}
    broker_pos_state: Dict[str, Any] = {
        "has_position": False,
        "direction": None,
        "entry_price": None,
    }

    def get_settings() -> Dict[str, Any]:
        return settings

    def get_contract_id() -> str:
        return contract_id

    def get_symbol_id() -> str:
        return symbol

    def get_account_id() -> int:
        return account_id

    def update_trade_display(trade: TradeState | None) -> None:
        if trade is None:
            _log("Trade cleared.")
            return
        _log(
            f"Trade state: dir={trade.direction} status={trade.status} "
            f"entry={trade.entry_price} sl={trade.sl_price} tps={trade.tp_prices}"
        )

    def set_last_price(val: float) -> None:
        last_price_holder["val"] = val

    loop = TradingLoopV1(
        client=client,
        log=_log,
        get_settings=get_settings,
        get_contract_id=get_contract_id,
        get_symbol_id=get_symbol_id,
        get_account_id=get_account_id,
        update_trade_display=update_trade_display,
        set_last_price=set_last_price,
        is_daily_locked=None,
    )

    # Keep a lightweight mirror of broker position state (UserHub) for periodic sync.
    def _on_position(p: UserPosition):
        try:
            if p.account_id is None or int(p.account_id) != int(account_id):
                return
            if not p.contract_id or str(p.contract_id) != str(contract_id):
                return

            # Update broker position snapshot used for periodic reconciliation.
            if not p.size or p.average_price is None:
                broker_pos_state["has_position"] = False
                broker_pos_state["direction"] = None
                broker_pos_state["entry_price"] = None
            else:
                broker_pos_state["has_position"] = True
                broker_pos_state["direction"] = "LONG" if p.type == 1 else "SHORT"
                broker_pos_state["entry_price"] = float(p.average_price)
        except Exception as e:
            _log(f"Recovery position handler error: {e}")

    user_hub = UserHubClient(token_factory=lambda: client.token or "", log=_log)
    user_hub.set_on_position(_on_position)
    user_hub.start()
    try:
        user_hub.subscribe_trades(account_id)
    except Exception:
        pass

    loop.start_live_watchdog()
    loop.start_signals()
    loop.start_live_feed()

    # Periodic reconciliation: once per minute compare broker position (UserHub)
    # against internal trade state and correct any drift (stuck-in-trade or
    # missing trade attachment).
    _log("Starting broker position reconciliation thread (60s interval).")
    stop = False

    def _reconcile_loop():
        while not stop:
            try:
                loop.sync_with_broker_position(
                    has_position=bool(broker_pos_state["has_position"]),
                    direction=broker_pos_state["direction"],
                    entry_price=broker_pos_state["entry_price"],
                )
            except Exception as e:
                _log(f"Broker reconcile error: {e}")
            for _ in range(60):
                if stop:
                    return
                time.sleep(1.0)

    reconcile_thread = threading.Thread(target=_reconcile_loop, daemon=False)
    reconcile_thread.start()

    _log("v5 CLI loop running. Press Ctrl+C to exit.")

    def _handle_sigint(signum, frame):
        nonlocal stop
        _log("Shutdown requested (signal).")
        stop = True

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    try:
        while not stop:
            time.sleep(1.0)
    finally:
        stop = True
        reconcile_thread.join(timeout=2.0)
        _log("Stopping signals and live feed...")
        loop.stop_signals()
        loop.stop_live_feed()
        _log("Exited cleanly.")


if __name__ == "__main__":
    main()


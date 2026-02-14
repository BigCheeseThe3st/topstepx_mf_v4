# TopstepX HF v5 (Live / Combine CLI)

Headless CLI for TopstepX running **TheBigCheese Gold Algo** (v1_5 logic) only, with a **fixed trade window** (00:00–05:30 UTC). Intended for live/combine runs on a Raspberry Pi or any machine without a GUI.

## Disclaimer

This software is provided “as is” with no warranties. Trading involves substantial risk and you can lose money. You are solely responsible for any losses or damages resulting from use of this tool. Use at your own risk.

## Usage Warning

Always start on a **practice/sim account**, validate behaviour, and only then consider a combine or live account. Use a stable, low-latency connection (wired preferred).

---

## Overview

- **Strategy:** **TheBigCheese Gold Algo** (v1_5) — 1m + 5m Bollinger Bands (8-period, 2.0 mult, close-only), enter on band touch + retrace; fixed SL/TP; cooldown after stop loss.
- **Trade window:** **00:00–05:30 UTC** — signals are only accepted inside this window.
- **No GUI:** CLI only; credentials and account selection via file, env vars, or prompts.
- **Recovery:** If the process restarts while a position is open, it reattaches via UserHub and recomputes TP/SL.
- **Trade log:** All opens and closes are written to `trades_log.csv` in this folder with millisecond timestamps.

v5 matches the behaviour of v5.1 (practice CLI) except for the enforced trade window; v5.1 has a 00:00–23:59 window by default.

---

## Quick Start

1. **Install dependencies** (from project root or wherever `topstepx` is available):
   - `python -m pip install -r requirements.txt` (if present), or install the packages required by `topstepx` (e.g. `requests`, `websocket-client`).

2. **Credentials** (one of):
   - Create `credentials.json` in this folder with `username` and `api_key`.
   - Set env: `TOPSTEPX_USERNAME`, `TOPSTEPX_API_KEY`.
   - Or enter them when prompted on first run (they are then saved to `credentials.json`).

3. **Run:**
   - `python main.py`
   - If no account is pre-selected (see below), choose an account from the numbered list.

4. **Contract:** The first Micro Gold (MGC) contract from the API is used; for combine/live this is typically the current front month.

---

## Configuration

Edit the `CONFIG` dict in `main.py`, or use environment variables where supported:

| Setting | Default | Env / notes |
|--------|---------|-------------|
| Order size (lots) | 3 | `TOPSTEPX_ORDER_SIZE` |
| Cooldown after SL | 55 s | In code |
| Trade window | 00:00–05:30 UTC | In code (`trade_windows`) |
| Preferred account | None | `TOPSTEPX_ACCOUNT_ID` — skip account prompt |

Strategy defaults (in the trading loop) include:

- **SL:** 50 ticks  
- **TP:** 100 ticks  
- **Bollinger:** length 8, mult 2.0, close-only, historical bars for band data  

---

## Account Selection

- If **`TOPSTEPX_ACCOUNT_ID`** is set and matches an active account, that account is used and no prompt is shown.
- Otherwise, the CLI lists active accounts (number, id, name, type) and asks you to pick by number (e.g. `1`). Blank defaults to the first account.

---

## Recovery and Trade Log

- **Recovery:** On startup, the app subscribes to UserHub for the chosen account. If it finds an open position for the selected contract, it calls `recover_from_position(direction, entry_price)`, attaches to that trade, and recomputes TP/SL in a background thread. No manual flatten is required to “reattach” after a crash.
- **Trade log:** Path is `topstepx_hf_v5/trades_log.csv`. Events logged:
  - **OPEN** — when a signal is accepted (includes `trigger_price`).
  - **CLOSE** — on SL or TP (includes `reason`, `exit_price`, `hit_tp_price`, etc.).  
  Timestamps are UTC with millisecond precision (`timespec="milliseconds"`).

---

## File Map

| File | Purpose |
|------|--------|
| `main.py` | CLI entrypoint; credentials, account/contract discovery, UserHub, recovery hook |
| `credentials.json` | Cached username/API key (created on first run or from env) |
| `trades_log.csv` | Trade events (OPEN/CLOSE) with millisecond timestamps |
| `topstepx/trading_loop_v1.py` | v1 loop: signals → entry/TP/SL, cooldown, recovery, trade logging |
| `topstepx/signal_engine.py` | v1 signals: 1m/5m Bollinger, band touch + retrace |

---

## Troubleshooting

- **No accounts / wrong account:** Ensure you’re logged in and the account is active. Use `TOPSTEPX_ACCOUNT_ID` to force an account without prompting.
- **No contracts:** API may return no contracts for your environment; the code falls back to contract search for “MGC” and then to the first contract if no MGC is found.
- **Recovery not attaching:** Check that UserHub is connected and that the open position is for the same account and contract the CLI is using.
- **Trade window:** v5 only takes signals between 00:00 and 05:30 UTC; outside that window, signals are ignored (no change to the window in this README; edit `main.py` if you need different hours).

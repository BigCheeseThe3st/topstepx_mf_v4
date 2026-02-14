# © 2026 BigCheeseThe3st
# Licensed under NCSAL v1.1 (see LICENSE.txt)
from __future__ import annotations
import json

import threading
import logging
from dataclasses import dataclass
from typing import Callable, Optional, List

from signalrcore.hub_connection_builder import HubConnectionBuilder
from signalrcore.messages.completion_message import CompletionMessage

# `signalrcore` has changed transport class names across versions.
# We try to import a websocket transport if available, but we can also
# rely on `skip_negotiation=True` to select websockets in many versions.
_WS_TRANSPORT = None
try:  # signalrcore <= some versions
    from signalrcore.transport.websockets import WebsocketTransport as _WS_TRANSPORT  # type: ignore
except Exception:
    try:  # other versions
        from signalrcore.transport.websockets import WebSocketsTransport as _WS_TRANSPORT  # type: ignore
    except Exception:
        try:  # other versions
            from signalrcore.transport.websockets import WebSocketTransport as _WS_TRANSPORT  # type: ignore
        except Exception:
            _WS_TRANSPORT = None

RTC_MARKET_HUB = "https://rtc.topstepx.com/hubs/market"

@dataclass
class Quote:
    symbol_id: str
    last_price: float | None
    best_bid: float | None
    best_ask: float | None
    timestamp: str | None
    raw: dict | None = None

class MarketHubClient:
    """
    SignalR Market Hub client.

    The JS docs connect like:
      https://rtc.topstepx.com/hubs/market?access_token=YOUR_JWT_TOKEN
      with skipNegotiation: true, transport: WebSockets, accessTokenFactory: () => JWT

    In python `signalrcore`, the closest equivalent is:
      HubConnectionBuilder().with_url(
          "https://rtc.topstepx.com/hubs/market",
          options={
              "access_token_factory": lambda: token,
              "skip_negotiation": True,
              "transport": WebsocketTransport
          }
      ).build()
    """
    def __init__(self, token_factory: Callable[[], str], log: Callable[[str], None]):
        self.token_factory = token_factory
        self.log = log
        self._hub = None
        self._thread: Optional[threading.Thread] = None
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._suppress_errors = False
        self._logger_levels: dict[str, int] = {}

        self._on_quote: Optional[Callable[[Quote], None]] = None
        self._pending_subs: List[str] = []

    def set_on_quote(self, cb: Callable[[Quote], None]):
        self._on_quote = cb

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._suppress_errors = False
        self._set_signalrcore_logging(False)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        # Suppress noisy websocket errors during intentional shutdown
        self._suppress_errors = True
        self._set_signalrcore_logging(True)
        try:
            if self._hub:
                self._hub.stop()
        except Exception:
            pass
        self._connected.clear()

    def _set_signalrcore_logging(self, suppress: bool):
        names = [
            "signalrcore",
            "signalrcore.transport.websockets.websocket_client",
            "signalrcore.transport.websockets.websocket_transport",
        ]
        if suppress:
            for name in names:
                logger = logging.getLogger(name)
                self._logger_levels[name] = logger.level
                logger.setLevel(logging.CRITICAL)
        else:
            for name, level in self._logger_levels.items():
                logging.getLogger(name).setLevel(level)
            self._logger_levels.clear()

    def subscribe_contract(self, contract_id: str):
        """Subscribe to quotes for a contractId (e.g. 'CON.F.US.GCE.J26')."""
        contract_id = str(contract_id)
        if contract_id not in self._pending_subs:
            self._pending_subs.append(contract_id)
        if self._connected.is_set():
            self._flush_subs()

    def _flush_subs(self):
        if not self._hub:
            return
        while self._pending_subs:
            contract_id = self._pending_subs.pop(0)
            try:
                # ProjectX docs: SubscribeContractQuotes(CONTRACT_ID)
                self._hub.send("SubscribeContractQuotes", [contract_id])
                self.log(f"Subscribed contract quotes: {contract_id}")
            except Exception as e:
                self.log(f"Subscribe failed ({contract_id}): {e}")

    def _run(self):
        try:
            def _handle_quote(args):
                """Handle a GatewayQuote event payload.

                signalrcore may deliver:
                  - a dict payload
                  - a JSON string payload
                  - a list/tuple of args like [contractId, payload]
                We normalize into a dict with at least lastPrice/bestBid/bestAsk where available.
                """
                try:
                    payload = args
                    # signalrcore commonly passes a list of args
                    if isinstance(args, (list, tuple)):
                        payload = args[-1] if len(args) else {}
                    # Sometimes the payload is still a string (JSON)
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except Exception:
                            # If it's not JSON, nothing to parse
                            payload = {"raw": payload}

                    # Some servers wrap payload
                    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], dict):
                        payload = payload["data"]

                    if not isinstance(payload, dict):
                        raise TypeError(f"unexpected payload type: {type(payload)}")

                    q = Quote(
                        symbol_id=payload.get("symbolId") or payload.get("symbol") or "",
                        last_price=payload.get("lastPrice"),
                        best_bid=payload.get("bestBid"),
                        best_ask=payload.get("bestAsk"),
                        timestamp=payload.get("timestamp") or payload.get("timeStamp") or payload.get("lastUpdated"),
                        raw=payload,
                    )
                    if self._on_quote:
                        self._on_quote(q)
                except Exception as e:
                    self.log(f"Quote parse error: {e}")

            # Some gateways require the access token to be present as a query param
            # (as in the official JS examples), even if an access_token_factory is
            # also provided. We therefore embed the current token into the URL.
            token = (self.token_factory() or "").strip()
            hub_url = RTC_MARKET_HUB
            # Official examples pass the token as a query string.
            if token:
                sep = "&" if "?" in hub_url else "?"
                hub_url = f"{hub_url}{sep}access_token={token}"

            options = {
                "access_token_factory": (lambda: (self.token_factory() or "").strip()),
                "skip_negotiation": True,
            }
            # Transport arg is optional and version-dependent in `signalrcore`.
            if _WS_TRANSPORT is not None:
                options["transport"] = _WS_TRANSPORT

            # Avoid logging sensitive token in URL
            safe_url = RTC_MARKET_HUB
            if token:
                safe_url = f"{RTC_MARKET_HUB}?access_token=***"
            self.log(f"Market hub connecting: {safe_url}")
            self._hub = HubConnectionBuilder().with_url(hub_url, options=options).build()

            def _on_open():
                self.log("Market hub connected.")
                self._connected.set()
                self._flush_subs()

            self._hub.on_open(_on_open)
            self._hub.on_close(lambda: self.log("Market hub closed."))
            def _on_error(data):
                if getattr(self, "_suppress_errors", False):
                    return
                # signalrcore may surface hub invocation errors as CompletionMessage
                if isinstance(data, CompletionMessage):
                    err = getattr(data, "error", None)
                    res = getattr(data, "result", None)
                    inv = getattr(data, "invocation_id", None)
                    self.log(f"Market hub error (CompletionMessage id={inv}): error={err} result={res}")
                else:
                    self.log(f"Market hub error: {data}")

            self._hub.on_error(_on_error)

            self._hub.on("GatewayQuote", _handle_quote)

            self._hub.start()

            while not self._stop.is_set():
                self._stop.wait(0.25)

        except Exception as e:
            if not getattr(self, "_suppress_errors", False):
                self.log(f"Market hub connect failed: {e}")
            self._connected.clear()

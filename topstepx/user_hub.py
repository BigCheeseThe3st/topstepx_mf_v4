from __future__ import annotations
import json

import threading
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from signalrcore.hub_connection_builder import HubConnectionBuilder
from signalrcore.messages.completion_message import CompletionMessage

# `signalrcore` has changed transport class names across versions.
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

RTC_USER_HUB = "https://rtc.topstepx.com/hubs/user"

@dataclass
class UserTrade:
    trade_id: int | None
    account_id: int | None
    contract_id: str | None
    creation_timestamp: str | None
    profit_and_loss: float | None
    fees: float | None
    side: int | None
    size: int | None
    voided: bool | None
    order_id: int | None
    raw: dict | None = None

@dataclass
class UserPosition:
    position_id: int | None
    account_id: int | None
    contract_id: str | None
    creation_timestamp: str | None
    type: int | None  # 1=Long, 2=Short
    size: int | None
    average_price: float | None
    raw: dict | None = None

class UserHubClient:
    def __init__(self, token_factory: Callable[[], str], log: Callable[[str], None]):
        self.token_factory = token_factory
        self.log = log
        self._hub = None
        self._thread: Optional[threading.Thread] = None
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._suppress_errors = False
        self._logger_levels: dict[str, int] = {}
        self._on_trade: Optional[Callable[[UserTrade], None]] = None
        self._on_position: Optional[Callable[[UserPosition], None]] = None
        self._account_id: Optional[int] = None

    def set_on_trade(self, cb: Callable[[UserTrade], None]):
        self._on_trade = cb

    def set_on_position(self, cb: Callable[[UserPosition], None]):
        self._on_position = cb

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
        self._suppress_errors = True
        self._set_signalrcore_logging(True)
        try:
            if self._hub:
                self._hub.stop()
        except Exception:
            pass
        self._connected.clear()

    def subscribe_trades(self, account_id: int):
        self._account_id = int(account_id)
        if self._connected.is_set():
            self._do_subscribe()

    def _do_subscribe(self):
        if not self._hub or self._account_id is None:
            return
        try:
            self._hub.send("SubscribeTrades", [int(self._account_id)])
            self._hub.send("SubscribePositions", [int(self._account_id)])
            self.log(f"Subscribed user trades: accountId={self._account_id}")
        except Exception as e:
            self.log(f"Subscribe trades failed: {e}")

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

    def _run(self):
        try:
            def _handle_trade(args):
                try:
                    payload = args
                    if isinstance(args, (list, tuple)):
                        payload = args[-1] if len(args) else {}
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except Exception:
                            payload = {"raw": payload}
                    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], dict):
                        payload = payload["data"]
                    if not isinstance(payload, dict):
                        raise TypeError(f"unexpected payload type: {type(payload)}")
                    t = UserTrade(
                        trade_id=payload.get("id"),
                        account_id=payload.get("accountId"),
                        contract_id=payload.get("contractId"),
                        creation_timestamp=payload.get("creationTimestamp") or payload.get("timestamp"),
                        profit_and_loss=payload.get("profitAndLoss"),
                        fees=payload.get("fees"),
                        side=payload.get("side"),
                        size=payload.get("size"),
                        voided=payload.get("voided"),
                        order_id=payload.get("orderId"),
                        raw=payload,
                    )
                    if self._on_trade:
                        self._on_trade(t)
                except Exception as e:
                    self.log(f"User trade parse error: {e}")

            def _handle_position(args):
                try:
                    payload = args
                    if isinstance(args, (list, tuple)):
                        payload = args[-1] if len(args) else {}
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload)
                        except Exception:
                            payload = {"raw": payload}
                    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], dict):
                        payload = payload["data"]
                    if not isinstance(payload, dict):
                        raise TypeError(f"unexpected payload type: {type(payload)}")
                    p = UserPosition(
                        position_id=payload.get("id"),
                        account_id=payload.get("accountId"),
                        contract_id=payload.get("contractId"),
                        creation_timestamp=payload.get("creationTimestamp") or payload.get("timestamp"),
                        type=payload.get("type"),
                        size=payload.get("size"),
                        average_price=payload.get("averagePrice"),
                        raw=payload,
                    )
                    if self._on_position:
                        self._on_position(p)
                except Exception as e:
                    self.log(f"User position parse error: {e}")

            token = (self.token_factory() or "").strip()
            hub_url = RTC_USER_HUB
            if token:
                sep = "&" if "?" in hub_url else "?"
                hub_url = f"{hub_url}{sep}access_token={token}"

            options = {
                "access_token_factory": (lambda: (self.token_factory() or "").strip()),
                "skip_negotiation": True,
            }
            if _WS_TRANSPORT is not None:
                options["transport"] = _WS_TRANSPORT

            safe_url = RTC_USER_HUB
            if token:
                safe_url = f"{RTC_USER_HUB}?access_token=***"
            self.log(f"User hub connecting: {safe_url}")
            self._hub = HubConnectionBuilder().with_url(hub_url, options=options).build()

            def _on_open():
                self.log("User hub connected.")
                self._connected.set()
                self._do_subscribe()

            self._hub.on_open(_on_open)
            self._hub.on_close(lambda: self.log("User hub closed."))

            def _on_error(data):
                if getattr(self, "_suppress_errors", False):
                    return
                if isinstance(data, CompletionMessage):
                    err = getattr(data, "error", None)
                    res = getattr(data, "result", None)
                    inv = getattr(data, "invocation_id", None)
                    self.log(f"User hub error (CompletionMessage id={inv}): error={err} result={res}")
                else:
                    self.log(f"User hub error: {data}")

            self._hub.on_error(_on_error)
            self._hub.on("GatewayUserTrade", _handle_trade)
            self._hub.on("GatewayUserPosition", _handle_position)
            self._hub.start()

            while not self._stop.is_set():
                self._stop.wait(0.25)
        except Exception as e:
            if not getattr(self, "_suppress_errors", False):
                self.log(f"User hub connect failed: {e}")
            self._connected.clear()

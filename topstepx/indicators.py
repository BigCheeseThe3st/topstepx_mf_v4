from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple
import pandas as pd
import numpy as np

@dataclass
class Bollinger:
    lower: float
    mid: float
    upper: float

def bollinger_bands(close: pd.Series, length: int = 10, mult: float = 1.5) -> Bollinger:
    """
    Standard Bollinger Bands: mid=SMA, upper=mid+mult*std, lower=mid-mult*std
    Uses population std (ddof=0) to match many trading platforms; adjust if needed.
    """
    if len(close) < length:
        raise ValueError("not enough data for bollinger")
    win = close.iloc[-length:]
    mid = float(win.mean())
    std = float(win.std(ddof=0))
    upper = mid + mult * std
    lower = mid - mult * std
    return Bollinger(lower=lower, mid=mid, upper=upper)

def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> float:
    """
    Average True Range (ATR) using Wilder's smoothing (simple mean over last length for simplicity).
    """
    if len(close) < length + 1:
        raise ValueError("not enough data for atr")
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return float(tr.iloc[-length:].mean())

def rsi(close: pd.Series, length: int = 14) -> float:
    """
    Relative Strength Index (RSI), simple average gains/losses.
    """
    if len(close) < length + 1:
        raise ValueError("not enough data for rsi")
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.iloc[-length:].mean()
    avg_loss = loss.iloc[-length:].mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

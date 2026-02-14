# © 2026 BigCheeseThe3st
# Licensed under NCSAL v1.1 (see LICENSE.txt)
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple


@dataclass
class CalendarEvent:
    date: str  # YYYY-MM-DD
    time: str  # HH:MM (UTC)
    name: str
    pre_min: int = 15
    post_min: int = 15


def load_events(path: str) -> List[CalendarEvent]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except Exception:
        return []
    events: List[CalendarEvent] = []
    for item in data or []:
        try:
            events.append(
                CalendarEvent(
                    date=str(item.get("date", "")).strip(),
                    time=str(item.get("time", "")).strip(),
                    name=str(item.get("name", "")).strip(),
                    pre_min=int(item.get("pre_min", 15)),
                    post_min=int(item.get("post_min", 15)),
                )
            )
        except Exception:
            continue
    return events


def save_events(path: str, events: List[CalendarEvent]) -> None:
    payload = [
        {
            "date": e.date,
            "time": e.time,
            "name": e.name,
            "pre_min": int(e.pre_min),
            "post_min": int(e.post_min),
        }
        for e in events
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _event_window(event: CalendarEvent) -> Optional[Tuple[datetime, datetime]]:
    try:
        dt = datetime.fromisoformat(f"{event.date}T{event.time}:00+00:00")
    except Exception:
        return None
    start = dt - timedelta(minutes=int(event.pre_min))
    end = dt + timedelta(minutes=int(event.post_min))
    return start, end


def is_blocked(now: datetime, events: List[CalendarEvent]) -> Optional[str]:
    now_utc = now.astimezone(timezone.utc)
    for event in events:
        window = _event_window(event)
        if not window:
            continue
        start, end = window
        if start <= now_utc <= end:
            label = event.name or "news"
            return f"{label} {event.date} {event.time} UTC"
    return None

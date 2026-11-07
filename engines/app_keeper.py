"""
engines/app_keeper.py — When SAP2000 / AutoCAD stay open on a small VM
======================================================================
Starting SAP2000 or AutoCAD costs far more than the work Penny asks of them,
but each holds ~0.5–1 GB of RAM. On an 8 GB VM they are kept open only when
that pays off:

  SAP2000  open all working hours (default Mon–Fri 09:00–17:00, VM local time),
           pre-started at the start of the day so the first job is quick.
           Outside working hours: started on demand, closed after
           SAP2000_OFF_HOURS_IDLE_MIN (default 10) idle minutes.
  AutoCAD  always on demand (ECM is occasional): closed after
           AUTOCAD_IDLE_MIN (default 5) idle minutes, which keeps it open
           across the DWGs of one ECM package.

Only an instance the worker started itself is ever closed (and AutoCAD only
with no drawing open), so a SAP2000 / AutoCAD session someone is using on the
VM is never shut down.

Environment:
  WORK_HOURS="09:00-17:00"   WORK_DAYS="Mon-Fri"
  SAP2000_PREWARM=1          SAP2000_OFF_HOURS_IDLE_MIN=10   AUTOCAD_IDLE_MIN=5
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Callable

log = logging.getLogger("penny_win_worker.app_keeper")

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _minutes(hhmm: str) -> int:
    h, _, m = hhmm.strip().partition(":")
    return int(h) * 60 + int(m or 0)


def parse_hours(spec: str) -> tuple[int, int]:
    """'09:00-17:00' -> (540, 1020) minutes after midnight."""
    start, _, end = spec.partition("-")
    return _minutes(start), _minutes(end)


def parse_days(spec: str) -> set[int]:
    """'Mon-Fri' / 'Mon,Wed,Sat' / 'Mon-Sat' -> {0..6} (Monday = 0)."""
    out: set[int] = set()
    for part in spec.lower().replace(" ", "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        ia, ib = _DAYS.index(a[:3]), _DAYS.index((b or a)[:3])
        k = ia
        while True:
            out.add(k)
            if k == ib:
                break
            k = (k + 1) % 7
    return out


def in_work_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    try:
        start, end = parse_hours(os.getenv("WORK_HOURS", "09:00-17:00"))
        days = parse_days(os.getenv("WORK_DAYS", "Mon-Fri"))
    except (ValueError, IndexError):
        start, end, days = 540, 1020, {0, 1, 2, 3, 4}
    m = now.hour * 60 + now.minute
    return now.weekday() in days and start <= m < end


def sap2000_idle_limit_s(now: datetime | None = None) -> float | None:
    """Seconds SAP2000 may sit idle before closing; None = keep open (working hours)."""
    if in_work_hours(now):
        return None
    return float(os.getenv("SAP2000_OFF_HOURS_IDLE_MIN", "10")) * 60.0


def autocad_idle_limit_s() -> float:
    return float(os.getenv("AUTOCAD_IDLE_MIN", "5")) * 60.0


def sap2000_prewarm_enabled() -> bool:
    return os.getenv("SAP2000_PREWARM", "1") != "0"


class Keeper:
    """Background loop that closes idle apps and pre-warms SAP2000 for the day."""

    def __init__(self, tasks: list[Callable[[], None]], interval_s: float = 60.0):
        self.tasks = tasks
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        for task in self.tasks:
            try:
                task()
            except Exception as e:  # never let housekeeping kill the worker
                log.warning(f"app keeper task {getattr(task, '__name__', task)} failed: {e}")

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.tick()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="app-keeper", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()


def idle_for(last_used: float, now: float | None = None) -> float:
    return (now if now is not None else time.monotonic()) - last_used

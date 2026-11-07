"""
engines/process_guard.py — Process Watchdog & Timeout Guard for Windows CAD/CAE
===============================================================================
Prevents hung SAP2000.exe, acad.exe, accoreconsole.exe, or ODAFileConverter.exe
processes from wedging the Windows worker queue when a modal dialog or singular
stiffness matrix freezes COM execution.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, TypeVar

log = logging.getLogger("penny_win_worker.process_guard")

T = TypeVar("T")

# Shared global lock across SAP2000 and AutoCAD COM operations so two heavy
# desktop COM servers never fight over Windows desktop/clipboard/COM resources
GLOBAL_ENGINE_LOCK = threading.Lock()


def kill_windows_processes(process_names: list[str]) -> list[str]:
    """Force-terminate any running Windows processes matching `process_names`.

    Safe no-op on non-Windows platforms (for unit testing).
    Returns list of process names that were targeted.
    """
    killed: list[str] = []
    if sys.platform != "win32":
        return killed

    for proc_name in process_names:
        try:
            res = subprocess.run(
                ["taskkill", "/F", "/T", "/IM", proc_name],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode == 0:
                log.warning(f"Terminated hung process: {proc_name}")
                killed.append(proc_name)
        except Exception as e:
            log.debug(f"taskkill for {proc_name} ignored: {e}")
    return killed


def run_with_watchdog(
    fn: Callable[..., T],
    *args: Any,
    timeout_seconds: int = 300,
    kill_on_timeout: list[str] | None = None,
    **kwargs: Any,
) -> T:
    """Run `fn(*args, **kwargs)` inside a single thread with a strict timeout.

    If `timeout_seconds` elapses before `fn` returns, `kill_windows_processes`
    is invoked on `kill_on_timeout` (e.g. `["SAP2000.exe"]` or `["acad.exe"]`)
    to break any hung COM RPC call, and `TimeoutError` is raised.
    """
    if timeout_seconds <= 0:
        return fn(*args, **kwargs)

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="com_watchdog")
    future: Future[T] = executor.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=timeout_seconds)
    except TimeoutError:
        targets = kill_on_timeout or []
        killed = kill_windows_processes(targets)
        log.error(
            f"Operation {getattr(fn, '__name__', str(fn))} timed out after "
            f"{timeout_seconds}s. Killed processes: {killed}"
        )
        raise TimeoutError(
            f"Execution timed out after {timeout_seconds}s "
            f"(terminated hung processes: {', '.join(killed) or 'none'})."
        )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

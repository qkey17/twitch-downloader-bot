"""Lightweight process metrics; all byte values are optional on unsupported OSes."""
import logging
import os
from pathlib import Path
from functools import lru_cache


@lru_cache(maxsize=1)
def _windows_api():
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage", "PrivateUsage",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    return ctypes, Counters, kernel, psapi


def memory_usage() -> dict:
    if os.name == "nt":
        try:
            ctypes, Counters, kernel, psapi = _windows_api()
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return {"rss_bytes": counters.WorkingSetSize, "process_peak_rss_bytes": counters.PeakWorkingSetSize,
                        "swap_bytes": None, "private_bytes": counters.PrivateUsage}
        except (OSError, AttributeError):
            pass
    else:
        try:
            status = Path("/proc/self/status").read_text(encoding="ascii")
            fields = {}
            for line in status.splitlines():
                name, _, value = line.partition(":")
                if name in {"VmRSS", "VmHWM", "VmSwap"}:
                    fields[name] = int(value.split()[0]) * 1024
            return {"rss_bytes": fields.get("VmRSS"), "process_peak_rss_bytes": fields.get("VmHWM"),
                    "swap_bytes": fields.get("VmSwap")}
        except (OSError, ValueError):
            pass
    return {"rss_bytes": None, "process_peak_rss_bytes": None, "swap_bytes": None}


def log_resources(stage, **metrics):
    logging.getLogger("tcd.resources").info(
        "stage=%s memory=%s metrics=%s peak_scope=process_lifetime", stage, memory_usage(), metrics,
    )

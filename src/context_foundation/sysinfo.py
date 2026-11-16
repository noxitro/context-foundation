"""計測の記録に添える、機材とメモリの情報(標準ライブラリだけ)。ホスト名や利用者名は取らない。"""
from __future__ import annotations

import os
import platform
import sys


def peak_memory_bytes() -> int | None:
    """このプロセスが使ったメモリの最大値(分からなければ None)。"""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            c = Counters()
            c.cb = ctypes.sizeof(c)
            if k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return int(c.PeakWorkingSetSize)
            return None
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    except Exception:
        return None


def cpu_name() -> str:
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def host_info() -> dict:
    return {"os": platform.system(), "os_release": platform.release(), "cpu": cpu_name(),
            "cpu_count": os.cpu_count(), "python": platform.python_version()}

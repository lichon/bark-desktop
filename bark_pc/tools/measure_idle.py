"""Idle footprint measurement (SC-006).

    python -m bark_pc.tools.measure_idle --duration 3600 --interval 30

Samples working set, private bytes and CPU for an existing process (or this
interpreter) every `--interval` seconds, writes a CSV plus a summary, and exits
non-zero when working set exceeds 150 MB or average CPU exceeds 1 %.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

WORKING_SET_LIMIT_MB = 150.0
CPU_LIMIT_PERCENT = 1.0


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


def _filetime_to_int(value: FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


class ProcessSampler:
    def __init__(self, pid: int):
        if sys.platform != "win32":
            raise SystemExit("measure_idle requires Windows")
        self.pid = int(pid)
        self.kernel32 = ctypes.windll.kernel32
        self.psapi = ctypes.windll.psapi
        self.handle = self.kernel32.OpenProcess(
            0x1000 | 0x0400, False, self.pid
        )  # QUERY_INFORMATION|VM_READ
        if not self.handle:
            raise SystemExit(f"cannot open process {self.pid}")
        self.last_cpu = self.cpu_seconds()

    def memory(self) -> tuple[float, float]:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        ok = self.psapi.GetProcessMemoryInfo(self.handle, ctypes.byref(counters), counters.cb)
        if not ok:
            return 0.0, 0.0
        return (
            counters.WorkingSetSize / (1024 * 1024),
            counters.PagefileUsage / (1024 * 1024),
        )

    def cpu_seconds(self) -> float:
        creation, exit_, kernel, user = (FILETIME() for _ in range(4))
        ok = self.kernel32.GetProcessTimes(
            self.handle,
            ctypes.byref(creation),
            ctypes.byref(exit_),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return 0.0
        return (_filetime_to_int(kernel) + _filetime_to_int(user)) / 10_000_000.0

    def sample(self, elapsed: float) -> tuple[float, float, float]:
        working_set, private = self.memory()
        cpu = self.cpu_seconds()
        delta = max(1e-6, cpu - self.last_cpu)
        self.last_cpu = cpu
        percent = (delta / elapsed) * 100.0
        return working_set, private, percent

    def close(self) -> None:
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


def run(pid: int, duration: float, interval: float, csv_path: Path) -> int:
    sampler = ProcessSampler(pid)
    rows: list[tuple[float, float, float, float]] = []
    started = time.monotonic()
    try:
        while True:
            elapsed_total = time.monotonic() - started
            if elapsed_total >= duration:
                break
            time.sleep(min(interval, max(0.1, duration - elapsed_total)))
            working_set, private, percent = sampler.sample(interval)
            rows.append((time.monotonic() - started, working_set, private, percent))
            print(
                f"{rows[-1][0]:8.0f}s  working set {working_set:7.1f} MB  "
                f"private {private:7.1f} MB  cpu {percent:5.2f} %"
            )
    finally:
        sampler.close()

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8") as handle:
        handle.write("elapsed_s,working_set_mb,private_mb,cpu_percent\n")
        for row in rows:
            handle.write(",".join(f"{value:.2f}" for value in row) + "\n")

    if not rows:
        print("no samples taken")
        return 1
    peak_ws = max(row[1] for row in rows)
    avg_cpu = sum(row[3] for row in rows) / len(rows)
    summary = (
        f"samples={len(rows)} peak_working_set={peak_ws:.1f} MB "
        f"average_cpu={avg_cpu:.2f} % (limits: {WORKING_SET_LIMIT_MB:.0f} MB, "
        f"{CPU_LIMIT_PERCENT:.0f} %)"
    )
    print(summary)
    with csv_path.open("a", encoding="utf-8") as handle:
        handle.write(f"# {summary}\n")
    ok = peak_ws <= WORKING_SET_LIMIT_MB and avg_cpu <= CPU_LIMIT_PERCENT
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, default=os.getpid())
    parser.add_argument("--duration", type=float, default=3600.0, help="seconds")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds")
    parser.add_argument("--csv", type=Path, default=Path("docs/validation/idle.csv"))
    args = parser.parse_args(argv)
    return run(args.pid, args.duration, args.interval, args.csv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

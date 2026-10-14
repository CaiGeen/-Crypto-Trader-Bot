"""Isolated Windows sharing/replace stress diagnostic.

This script writes only to a newly-created directory under the system temp
folder. It does not access production paths or use network services.

Usage: python reproduce_atomic_replace.py [--duration 1800]
The default and maximum duration is 30 minutes. This is a mechanism test, not a
production failure-rate estimate. Do not point it at a production directory.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import tempfile
import threading
import time
from collections import Counter


def _replace_error(errors, scenario, exc):
    errors[(scenario, type(exc).__name__, getattr(exc, "winerror", None),
            getattr(exc, "errno", None))] += 1


def _write_temp(root, prefix, payload):
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=root)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _no_delete_share_calibration(root, target, errors):
    result = {"attempts": 0, "expected_access_denied": 0,
              "other_failures": 0, "release_success": 0}
    if os.name != "nt":
        return result

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                            ctypes.c_void_p]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    invalid_handle = ctypes.c_void_p(-1).value

    for number in range(25):
        source = _write_temp(root, ".cal_", f'{{"cal":{number}}}\n'.encode())
        handle = create_file(
            target,
            0x80000000,                 # GENERIC_READ
            0x00000001 | 0x00000002,     # FILE_SHARE_READ | FILE_SHARE_WRITE
            None,
            3,                          # OPEN_EXISTING
            0x00000080,                 # FILE_ATTRIBUTE_NORMAL
            None,
        )
        result["attempts"] += 1
        try:
            try:
                os.replace(source, target)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 5:
                    result["expected_access_denied"] += 1
                else:
                    result["other_failures"] += 1
                    _replace_error(errors, "calibration", exc)
            else:
                result["other_failures"] += 1
        finally:
            if handle != invalid_handle:
                close_handle(handle)
            try:
                os.remove(source)
            except OSError:
                pass

        # The ordinary-reader thread remains active, matching the original
        # run. This is consequently not a no-reader release control.
        source = _write_temp(root, ".release_", b'{"after_release":true}\n')
        try:
            try:
                os.replace(source, target)
                result["release_success"] += 1
            except OSError as exc:
                _replace_error(errors, "release", exc)
        finally:
            try:
                os.remove(source)
            except OSError:
                pass
    return result


def run(duration):
    temp_base = os.path.join(tempfile.gettempdir(), "opencode")
    os.makedirs(temp_base, exist_ok=True)
    root = tempfile.mkdtemp(prefix="health-replace-repro-", dir=temp_base)
    target = os.path.join(root, "control.json")
    with open(target, "wb") as stream:
        stream.write(b'{"sequence":0}\n')
        stream.flush()
        os.fsync(stream.fileno())

    stop = threading.Event()
    counts = Counter()
    errors = Counter()
    counter_lock = threading.Lock()

    def reader():
        while not stop.is_set():
            try:
                with open(target, "rb") as stream:
                    stream.read()
                with counter_lock:
                    counts["reader_ok"] += 1
            except OSError as exc:
                with counter_lock:
                    counts["reader_error"] += 1
                    _replace_error(errors, "reader", exc)

    reader_thread = threading.Thread(target=reader, name="ordinary-reader", daemon=True)
    reader_thread.start()
    calibration = _no_delete_share_calibration(root, target, errors)

    start = time.monotonic()
    held = {"attempts": 0, "success": 0}
    while time.monotonic() - start < duration:
        source = _write_temp(
            root, ".writer_",
            json.dumps({"n": counts["normal_attempts"]}, separators=(",", ":")).encode(),
        )
        with counter_lock:
            counts["normal_attempts"] += 1
        try:
            try:
                os.replace(source, target)
                with counter_lock:
                    counts["normal_success"] += 1
            except OSError as exc:
                with counter_lock:
                    counts["normal_failure"] += 1
                    _replace_error(errors, "normal", exc)
        finally:
            try:
                os.remove(source)
            except OSError:
                pass

        source = _write_temp(root, ".held_", b'{"held_reader":true}\n')
        try:
            with open(target, "rb"):
                held["attempts"] += 1
                try:
                    os.replace(source, target)
                    held["success"] += 1
                except OSError as exc:
                    with counter_lock:
                        counts["held_failure"] += 1
                        _replace_error(errors, "held_python_reader", exc)
        finally:
            try:
                os.remove(source)
            except OSError:
                pass

        if counts["normal_attempts"] % 10_000 == 0:
            with open(os.path.join(root, "progress.json"), "w", encoding="utf-8") as stream:
                json.dump({"elapsed_s": round(time.monotonic() - start, 1),
                           "counts": dict(counts), "held": held}, stream)

    elapsed = time.monotonic() - start
    stop.set()
    reader_thread.join(timeout=5)
    summary = {
        "duration_s": round(elapsed, 2),
        "isolated_dir": root,
        "calibration_no_delete_share": calibration,
        "held_python_reader": held,
        "counts": dict(counts),
        "errors_by_scenario_type_winerror_errno": [
            {"scenario": key[0], "type": key[1], "winerror": key[2],
             "errno": key[3], "count": value}
            for key, value in errors.items()
        ],
        "target_exists": os.path.exists(target),
    }
    with open(os.path.join(root, "summary.json"), "w", encoding="utf-8") as stream:
        json.dump(summary, stream, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=1800,
                        help="duration in seconds (1..1800; default 1800)")
    args = parser.parse_args()
    if not 1 <= args.duration <= 1800:
        parser.error("--duration must be between 1 and 1800 seconds")
    run(args.duration)

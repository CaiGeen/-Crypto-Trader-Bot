# -*- coding: utf-8 -*-
"""业务活性协议：控制面与活跃批次进度（纯标准库、原子写入）。"""
import json
import os
import tempfile
import time
import uuid

HEALTH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot_health")
CONTROL_FILE = os.path.join(HEALTH_DIR, "control.json")
STALL_SECONDS = 900

def new_instance_id():
    return uuid.uuid4().hex

def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".health_", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try: os.remove(tmp)
        except OSError: pass

def write_progress(kind, instance_id, batch_id=None, symbol=None, sequence=0):
    name = "control.json" if kind == "control" else f"batch_{batch_id}.json"
    path = os.path.join(HEALTH_DIR, name)
    _atomic_write(path, {"kind": kind, "instance_id": instance_id, "batch_id": batch_id,
                          "symbol": symbol, "sequence": sequence, "ts": time.time()})
    return path

def remove_batch(instance_id, batch_id):
    path = os.path.join(HEALTH_DIR, f"batch_{batch_id}.json")
    try:
        with open(path, encoding="utf-8") as f: data = json.load(f)
        if data.get("instance_id") == instance_id: os.remove(path)
    except (OSError, ValueError): pass

def current_instance_id():
    try:
        with open(CONTROL_FILE, encoding="utf-8") as f:
            return json.load(f).get("instance_id")
    except (OSError, ValueError):
        return None

def read_progress(kind, instance_id, batch_id=None):
    name = "control.json" if kind == "control" else f"batch_{batch_id}.json"
    path = os.path.join(HEALTH_DIR, name)
    try:
        with open(path, encoding="utf-8") as f: data = json.load(f)
        if data.get("instance_id") != instance_id: return None
        return data
    except (OSError, ValueError): return None

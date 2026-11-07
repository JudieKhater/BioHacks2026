"""
Serial bridge: reads the ESP32 badge over USB and forwards to the backend.

The firmware prints one JSON line about every 100 ms, for example:
    {"effort": 412.5, "pressure": 3.2}
- effort    -> POST /api/sensor       (throat effort, merged into features)
- pressure  -> POST /api/emst/sample  (live breathing-trainer meter)

Usage:
    python client/serial_bridge.py --port /dev/tty.usbserial-0001   (Mac)
    python client/serial_bridge.py --port COM5                      (Windows)
    python client/serial_bridge.py --list
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request

SERVER = os.getenv("RECAP_SERVER", "http://localhost:8000")
TOKEN = os.getenv("RECAP_TOKEN", "")


def post(path, body):
    req = urllib.request.Request(SERVER + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Recap-Token": TOKEN})
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception as e:
        print(f"[post {path}] {e}")


def main():
    import serial  # pip install pyserial
    from serial.tools import list_ports

    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list or not args.port:
        for p in list_ports.comports():
            print(p.device, "-", p.description)
        return

    ser = serial.Serial(args.port, args.baud, timeout=1)
    print(f"Reading {args.port}…")
    last_effort_post = 0.0
    efforts = []
    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "effort" in d:
            efforts.append(float(d["effort"]))
            # average effort over ~1 s before sending, to keep traffic light
            if time.time() - last_effort_post > 1.0:
                post("/api/sensor", {"effort": sum(efforts) / len(efforts)})
                efforts, last_effort_post = [], time.time()
        if "pressure" in d:
            post("/api/emst/sample", {"pressure_cmh2o": float(d["pressure"])})


if __name__ == "__main__":
    main()

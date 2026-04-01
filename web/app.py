#!/usr/bin/env python3
"""Web GUI for Audio Router."""

import os
import select
import signal
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request

ROUTER_DIR = Path(__file__).parent.parent
AUDIOTEE_PATH = ROUTER_DIR / "bin" / "audiotee"
ROUTER_SCRIPT = ROUTER_DIR / "bin" / "audio_router.py"

app = Flask(__name__, template_folder=str(ROUTER_DIR / "web" / "templates"))

router_process = None
router_lock = threading.Lock()
router_status = {"running": False, "pid": None, "error": None}


def get_devices():
    import sounddevice as sd

    devices = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            devices.append(
                {
                    "id": i,
                    "name": d["name"],
                    "channels": d["max_output_channels"],
                    "rate": int(d["default_samplerate"])
                    if d["default_samplerate"]
                    else None,
                }
            )
    return devices


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/devices")
def api_devices():
    return jsonify(get_devices())


@app.route("/api/status")
def api_status():
    global router_status
    with router_lock:
        if router_process and router_process.poll() is not None:
            router_status["running"] = False
            router_status["pid"] = None
        return jsonify(router_status.copy())


@app.route("/api/start", methods=["POST"])
def api_start():
    global router_process, router_status

    with router_lock:
        if router_status["running"]:
            return jsonify({"error": "Already running"}), 400

        data = request.get_json(force=True, silent=True) or {}
        full = data.get("full")
        bass = data.get("bass")
        cutoff = data.get("cutoff", 80)
        delay = data.get("delay", 150)
        rate = data.get("rate", 48000)
        mute = data.get("mute", True)

        if full is None or bass is None:
            return jsonify({"error": "full and bass device IDs required"}), 400

        cmd = [
            sys.executable,
            str(ROUTER_SCRIPT),
            "--full",
            str(full),
            "--bass",
            str(bass),
            "--cutoff",
            str(cutoff),
            "--delay",
            str(delay),
            "--rate",
            str(rate),
        ]
        if not mute:
            cmd.append("--no-mute")

        try:
            router_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            router_status = {
                "running": True,
                "pid": router_process.pid,
                "error": None,
                "config": {
                    "full": full,
                    "bass": bass,
                    "cutoff": cutoff,
                    "delay": delay,
                    "rate": rate,
                    "mute": mute,
                },
            }
            return jsonify({"ok": True, "pid": router_process.pid})
        except Exception as e:
            router_status["error"] = "Failed to start router"
            return jsonify({"error": "Failed to start router"}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    global router_process, router_status

    with router_lock:
        if not router_status["running"]:
            return jsonify({"error": "Not running"}), 400

        if router_process:
            router_process.send_signal(signal.SIGINT)
            try:
                router_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                router_process.kill()
                router_process.wait()

        router_status = {"running": False, "pid": None, "error": None}
        return jsonify({"ok": True})


@app.route("/api/logs")
def api_logs():
    global router_process
    if router_process and router_status["running"]:
        try:
            fd = router_process.stdout.fileno()
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                line = router_process.stdout.readline()
                if line:
                    return jsonify({"line": line.strip()})
        except Exception:
            pass
    return jsonify({"line": None})


def main():
    port = int(os.environ.get("PORT", 8080))
    print(f"Starting Audio Router GUI on http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Web GUI for Audio Router."""

import json
import math
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from flask import Flask, jsonify, render_template, request

ROUTER_DIR = Path(__file__).parent.parent
AUDIOTEE_PATH = ROUTER_DIR / "bin" / "audiotee"
ROUTER_SCRIPT = ROUTER_DIR / "bin" / "audio_router.py"

app = Flask(__name__, template_folder=str(ROUTER_DIR / "web" / "templates"))

router_process = None
router_lock = threading.Lock()
router_status = {"running": False, "pid": None, "error": None}

metronome_thread = None
metronome_lock = threading.Lock()
metronome_running = False
metronome_config = {
    "bpm": 120,
    "full_device": None,
    "bass_device": None,
    "full_volume": 0.8,
    "bass_volume": 0.8,
}


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


def generate_click(sample_rate=48000, duration_ms=50, freq=1000):
    t = np.linspace(0, duration_ms / 1000, int(sample_rate * duration_ms / 1000), False)
    click = np.sin(2 * np.pi * freq * t)
    envelope = np.exp(-t * 80)
    return (click * envelope).astype(np.float32)


def metronome_loop():
    global metronome_running
    sample_rate = 48000
    click = generate_click(sample_rate)
    click_bass = generate_click(sample_rate, freq=200)

    full_stream = None
    bass_stream = None

    try:
        full_stream = sd.OutputStream(
            device=metronome_config["full_device"],
            samplerate=sample_rate,
            channels=1,
            latency="low",
        )
        bass_stream = sd.OutputStream(
            device=metronome_config["bass_device"],
            samplerate=sample_rate,
            channels=1,
            latency="low",
        )
        full_stream.start()
        bass_stream.start()

        beat = 0
        while metronome_running:
            interval = 60.0 / metronome_config["bpm"]
            vol = metronome_config["full_volume"]
            bass_vol = metronome_config["bass_volume"]

            accent = 1.5 if beat % 4 == 0 else 1.0

            full_stream.write(click.reshape(-1, 1) * vol * accent)
            bass_stream.write(click_bass.reshape(-1, 1) * bass_vol * accent)

            beat += 1
            time.sleep(interval)
    except Exception:
        pass
    finally:
        if full_stream:
            full_stream.stop()
            full_stream.close()
        if bass_stream:
            bass_stream.stop()
            bass_stream.close()


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
        except Exception:
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


@app.route("/api/delay", methods=["POST"])
def api_delay():
    data = request.get_json(force=True, silent=True) or {}
    delay_ms = data.get("delay_ms")
    if delay_ms is None:
        return jsonify({"error": "delay_ms required"}), 400
    try:
        config_dir = os.path.expanduser("~/.audio-router")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.json")
        config = {}
        if os.path.exists(config_path):
            with open(config_path) as f:
                config = json.load(f)
        config["delay_ms"] = int(delay_ms)
        with open(config_path, "w") as f:
            json.dump(config, f)
        return jsonify({"ok": True, "delay_ms": config["delay_ms"]})
    except Exception:
        return jsonify({"error": "Failed to update delay"}), 500


@app.route("/api/metronome/status")
def api_metronome_status():
    return jsonify(
        {
            "running": metronome_running,
            "bpm": metronome_config["bpm"],
            "full_device": metronome_config["full_device"],
            "bass_device": metronome_config["bass_device"],
            "full_volume": metronome_config["full_volume"],
            "bass_volume": metronome_config["bass_volume"],
        }
    )


@app.route("/api/metronome/start", methods=["POST"])
def api_metronome_start():
    global metronome_thread, metronome_running

    with metronome_lock:
        if metronome_running:
            return jsonify({"error": "Already running"}), 400

        data = request.get_json(force=True, silent=True) or {}
        metronome_config["bpm"] = data.get("bpm", 120)
        metronome_config["full_device"] = data.get("full_device")
        metronome_config["bass_device"] = data.get("bass_device")
        metronome_config["full_volume"] = data.get("full_volume", 0.8)
        metronome_config["bass_volume"] = data.get("bass_volume", 0.8)

        if (
            metronome_config["full_device"] is None
            or metronome_config["bass_device"] is None
        ):
            return jsonify({"error": "full_device and bass_device required"}), 400

        metronome_running = True
        metronome_thread = threading.Thread(target=metronome_loop, daemon=True)
        metronome_thread.start()
        return jsonify({"ok": True})


@app.route("/api/metronome/stop", methods=["POST"])
def api_metronome_stop():
    global metronome_running

    with metronome_lock:
        if not metronome_running:
            return jsonify({"error": "Not running"}), 400
        metronome_running = False
        return jsonify({"ok": True})


@app.route("/api/metronome/bpm", methods=["POST"])
def api_metronome_bpm():
    global metronome_running
    data = request.get_json(force=True, silent=True) or {}
    bpm = data.get("bpm")
    if bpm is None:
        return jsonify({"error": "bpm required"}), 400
    metronome_config["bpm"] = int(bpm)
    return jsonify({"ok": True, "bpm": metronome_config["bpm"]})


def main():
    port = int(os.environ.get("PORT", 8080))
    print(f"Starting Audio Router GUI on http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()

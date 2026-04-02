#!/usr/bin/env python3
"""Audio Router: splits system audio into full-range + bass-only outputs."""

import numpy as np
import sounddevice as sd
from scipy.signal import butter, sosfilt
import subprocess
import argparse
import sys
import os
import json
import time
import select
import fcntl


DEFAULT_BASS_CUTOFF = 80
DEFAULT_DELAY_MS = 150
DEFAULT_SAMPLE_RATE = 48000
AUDIOTEE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audiotee")
CONFIG_DIR = os.path.expanduser("~/.audio-router")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def _ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def _read_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


class DelayConfig:
    def __init__(self):
        self._delay_ms = DEFAULT_DELAY_MS
        self._last_check = 0
        self._interval = 0.2

    def get(self):
        now = time.time()
        if now - self._last_check > self._interval:
            self._delay_ms = _read_config().get("delay_ms", DEFAULT_DELAY_MS)
            self._last_check = now
        return self._delay_ms


class AudioRouter:
    def __init__(
        self,
        full_output_device,
        bass_output_device,
        sample_rate=DEFAULT_SAMPLE_RATE,
        bass_cutoff=DEFAULT_BASS_CUTOFF,
        delay_ms=DEFAULT_DELAY_MS,
        mute=True,
    ):
        self.sample_rate = sample_rate
        self.bass_cutoff = bass_cutoff
        self.delay_config = DelayConfig()

        self.delay_samples = max(0, int(delay_ms * sample_rate / 1000))
        self.delay_buffer = np.zeros((self.delay_samples + 48000, 2), dtype=np.float32)
        self.delay_write_pos = 0

        self.sos = butter(2, bass_cutoff, btype="low", fs=sample_rate, output="sos")
        self.zi = None

        self.full_output_device = full_output_device
        self.bass_output_device = bass_output_device
        self.mute = mute

        self.full_stream = None
        self.bass_stream = None
        self.running = False
        self.audiotee_proc = None

        print(f"Full Output:    {self._dname(full_output_device)}")
        print(f"Bass Output:    {self._dname(bass_output_device)}")
        print(f"Bass Cutoff:    {bass_cutoff} Hz")
        print(f"Sync Delay:     {delay_ms} ms")
        print(f"Sample Rate:    {sample_rate} Hz")
        print(f"Mute Tapped:    {mute}")
        print()

    def _dname(self, did):
        try:
            return sd.query_devices(did)["name"]
        except Exception:
            return f"Device {did}"

    def process_chunk(self, audio):
        delay_ms = self.delay_config.get()
        new_delay = max(0, int(delay_ms * self.sample_rate / 1000))
        if new_delay != self.delay_samples:
            self.delay_samples = new_delay

        n = audio.shape[0]
        delayed_full = np.zeros_like(audio)
        for i in range(n):
            read_pos = (self.delay_write_pos - self.delay_samples + i) % len(
                self.delay_buffer
            )
            delayed_full[i] = self.delay_buffer[read_pos]
            self.delay_buffer[(self.delay_write_pos + i) % len(self.delay_buffer)] = (
                audio[i]
            )
        self.delay_write_pos = (self.delay_write_pos + n) % len(self.delay_buffer)

        if self.zi is None:
            n_sections = self.sos.shape[0]
            self.zi = np.zeros((n_sections, 2, 2))
        bass = sosfilt(self.sos, audio, axis=0, zi=self.zi)[0].astype(np.float32)

        return delayed_full, bass

    def start_audiotee(self):
        cmd = [AUDIOTEE_PATH, "--stereo"]
        if self.mute:
            cmd.append("--mute")
        if self.sample_rate != 48000:
            cmd.extend(["--sample-rate", str(self.sample_rate)])

        self.audiotee_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )

        fd = self.audiotee_proc.stderr.fileno()
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        for _ in range(20):
            try:
                line = self.audiotee_proc.stderr.readline()
                if line:
                    msg = json.loads(line.decode())
                    if msg.get("message_type") == "metadata":
                        meta = msg.get("data", {})
                        print(
                            f"Capture: {meta.get('sample_rate')}Hz, {meta.get('channels_per_frame')}ch"
                        )
                        return True
                    if msg.get("message_type") == "stream_start":
                        return True
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            time.sleep(0.2)
        return False

    def read_audio(self, min_bytes):
        partial = b""
        while self.running:
            if len(partial) >= min_bytes:
                result = partial[:min_bytes]
                partial = partial[min_bytes:]
                return result

            try:
                chunk = self.audiotee_proc.stdout.read(16384)
                if not chunk:
                    time.sleep(0.01)
                    continue
                partial += chunk
            except Exception:
                time.sleep(0.01)
        return None

    def run(self):
        self.running = True

        print("Starting audiotee...")
        if not self.start_audiotee():
            print("Failed to start audiotee")
            return
        print("Running. Ctrl+C to stop.\n")

        self.full_stream = sd.OutputStream(
            device=self.full_output_device,
            samplerate=self.sample_rate,
            channels=2,
            latency="low",
        )
        self.bass_stream = sd.OutputStream(
            device=self.bass_output_device,
            samplerate=self.sample_rate,
            channels=2,
            latency="low",
        )
        self.full_stream.start()
        self.bass_stream.start()

        bps = 4
        spc = 480
        bpc = spc * 2 * bps

        try:
            while self.running:
                raw = self.read_audio(bpc)
                if raw is None:
                    continue
                audio = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)
                full, bass = self.process_chunk(audio)
                self.full_stream.write(np.ascontiguousarray(full))
                self.bass_stream.write(np.ascontiguousarray(bass))
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            self.running = False
            if self.full_stream:
                self.full_stream.stop()
                self.full_stream.close()
            if self.bass_stream:
                self.bass_stream.stop()
                self.bass_stream.close()
            if self.audiotee_proc:
                self.audiotee_proc.terminate()
                try:
                    self.audiotee_proc.wait(timeout=3)
                except Exception:
                    self.audiotee_proc.kill()


def list_devices():
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            rate = int(d["default_samplerate"]) if d["default_samplerate"] else "N/A"
            print(
                f"{i:<4} {d['name'][:30]:<30} out={d['max_output_channels']}  rate={rate}"
            )


def main():
    p = argparse.ArgumentParser(description="Audio Router")
    p.add_argument("--list", action="store_true")
    p.add_argument("--full", type=int)
    p.add_argument("--bass", type=int)
    p.add_argument("--cutoff", type=int, default=DEFAULT_BASS_CUTOFF)
    p.add_argument("--delay", type=int, default=DEFAULT_DELAY_MS)
    p.add_argument("--rate", type=int, default=DEFAULT_SAMPLE_RATE)
    p.add_argument("--no-mute", action="store_true")
    args = p.parse_args()

    if args.list:
        list_devices()
        return

    if args.full is None or args.bass is None:
        p.print_help()
        print("\nError: --full and --bass required.")
        sys.exit(1)

    _ensure_config_dir()
    cfg = _read_config()
    cfg["delay_ms"] = args.delay
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f)

    AudioRouter(
        full_output_device=args.full,
        bass_output_device=args.bass,
        sample_rate=args.rate,
        bass_cutoff=args.cutoff,
        delay_ms=args.delay,
        mute=not args.no_mute,
    ).run()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Audio Router: splits system audio into full-range + bass-only outputs."""

import numpy as np
import sounddevice as sd
from scipy.signal import butter, sosfilt
import subprocess
import threading
import queue
import argparse
import sys
import os
import json


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


def _get_live_delay_ms():
    return _read_config().get("delay_ms", DEFAULT_DELAY_MS)


class DelayConfig:
    def __init__(self):
        self._delay_ms = DEFAULT_DELAY_MS
        self._last_check = 0
        self._interval = 0.2

    def get(self):
        import time

        now = time.time()
        if now - self._last_check > self._interval:
            self._delay_ms = _read_config().get("delay_ms", DEFAULT_DELAY_MS)
            self._last_check = now
        return self._delay_ms


class AudioteeCapture:
    def __init__(self, sample_rate=DEFAULT_SAMPLE_RATE, mute=True):
        self.sample_rate = sample_rate
        self.mute = mute
        self.proc = None
        self.pcm_queue = queue.Queue(maxsize=32)
        self.running = False
        self._partial = b""

    def start(self):
        cmd = [AUDIOTEE_PATH, "--stereo"]
        if self.mute:
            cmd.append("--mute")
        if self.sample_rate != 48000:
            cmd.extend(["--sample-rate", str(self.sample_rate)])

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )

        self.running = True
        threading.Thread(target=self._read_pcm, daemon=True).start()
        return self._read_metadata()

    def _read_metadata(self):
        import select

        fd = self.proc.stderr.fileno()
        while self.running and self.proc.poll() is None:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            line = self.proc.stderr.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode())
                if msg.get("message_type") == "metadata":
                    return msg.get("data", {})
                if msg.get("message_type") == "stream_start":
                    return None
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
        return None

    def _read_pcm(self):
        while self.running:
            chunk = self.proc.stdout.read(16384)
            if not chunk:
                break
            try:
                self.pcm_queue.put_nowait(chunk)
            except queue.Full:
                self.pcm_queue.get_nowait()
                self.pcm_queue.put_nowait(chunk)

    def read(self, min_bytes=16384):
        while self.running:
            if len(self._partial) >= min_bytes:
                result = self._partial[:min_bytes]
                self._partial = self._partial[min_bytes:]
                return result
            try:
                chunk = self.pcm_queue.get(timeout=1.0)
                self._partial += chunk
            except queue.Empty:
                continue
        return None

    def stop(self):
        self.running = False
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()


class DelayLine:
    def __init__(self, max_samples, channels=2):
        self.buffer = np.zeros((max_samples, channels), dtype=np.float32)
        self.max_samples = max_samples
        self.write_pos = 0
        self.delay_samples = 1

    def set_delay(self, samples):
        self.delay_samples = max(1, samples)

    def process(self, audio):
        n = audio.shape[0]
        output = np.zeros_like(audio)
        for i in range(n):
            read_pos = (self.write_pos - self.delay_samples + i) % self.max_samples
            output[i] = self.buffer[read_pos]
            self.buffer[(self.write_pos + i) % self.max_samples] = audio[i]
        self.write_pos = (self.write_pos + n) % self.max_samples
        return output


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
        self.delay_ms = delay_ms

        max_delay = int(1000 * sample_rate / 1000)
        self.delay = DelayLine(max_delay)
        self.delay.set_delay(int(delay_ms * sample_rate / 1000))

        self.sos = butter(2, bass_cutoff, btype="low", fs=sample_rate, output="sos")
        self.zi = None

        self.delay_config = DelayConfig()
        self.full_output_device = full_output_device
        self.bass_output_device = bass_output_device
        self.mute = mute

        self.full_queue = queue.Queue(maxsize=16)
        self.bass_queue = queue.Queue(maxsize=16)
        self.running = False

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
        live_ms = self.delay_config.get()
        self.delay.set_delay(int(live_ms * self.sample_rate / 1000))

        delayed_full = self.delay.process(audio)

        if self.zi is None:
            n_sections = self.sos.shape[0]
            self.zi = np.zeros((n_sections, 2, 2))
        bass = sosfilt(self.sos, audio, axis=0, zi=self.zi)[0].astype(np.float32)

        return delayed_full, bass

    def _output_thread(self, device_id, q):
        stream = sd.OutputStream(
            device=device_id, samplerate=self.sample_rate, channels=2, latency="low"
        )
        stream.start()
        try:
            while self.running:
                try:
                    chunk = q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                stream.write(np.ascontiguousarray(chunk))
        finally:
            stream.stop()
            stream.close()

    def run(self):
        self.running = True

        capture = AudioteeCapture(sample_rate=self.sample_rate, mute=self.mute)
        print("Starting audiotee...")
        meta = capture.start()
        if meta:
            print(
                f"Capture: {meta.get('sample_rate')}Hz, {meta.get('channels_per_frame')}ch"
            )
        print("Running. Ctrl+C to stop.\n")

        ft = threading.Thread(
            target=self._output_thread,
            args=(self.full_output_device, self.full_queue),
            daemon=True,
        )
        bt = threading.Thread(
            target=self._output_thread,
            args=(self.bass_output_device, self.bass_queue),
            daemon=True,
        )
        ft.start()
        bt.start()

        bps = 4
        spc = int(self.sample_rate * 0.05)
        bpc = spc * 2 * bps

        try:
            while self.running:
                raw = capture.read(bpc)
                if raw is None:
                    continue
                audio = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)
                full, bass = self.process_chunk(audio)
                try:
                    self.full_queue.put_nowait(full)
                except queue.Full:
                    self.full_queue.get_nowait()
                    self.full_queue.put_nowait(full)
                try:
                    self.bass_queue.put_nowait(bass)
                except queue.Full:
                    self.bass_queue.get_nowait()
                    self.bass_queue.put_nowait(bass)
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            self.running = False
            for q in [self.full_queue, self.bass_queue]:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            ft.join(timeout=2)
            bt.join(timeout=2)
            capture.stop()


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

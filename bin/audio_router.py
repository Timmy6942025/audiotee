#!/usr/bin/env python3
"""
Audio Router: Splits system audio into two outputs
- Full range → MacBook Pro Speakers
- Bass only (<80Hz) → WKing D8 Mini (Bluetooth speaker)
- Configurable delay to sync Bluetooth latency

Uses audiotee (Core Audio Taps API) for system audio capture — no sudo needed.
"""

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
AUDIOTEE_PATH = os.path.expanduser("~/Documents/audiotee/.build/release/audiotee")


class AudioteeCapture:
    def __init__(self, sample_rate=DEFAULT_SAMPLE_RATE, mute=True):
        self.sample_rate = sample_rate
        self.mute = mute
        self.proc = None
        self.pcm_queue = queue.Queue(maxsize=16)
        self.running = False
        self._partial_buffer = b""

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
        self.read_thread = threading.Thread(target=self._read_pcm, daemon=True)
        self.read_thread.start()

        metadata_line = self._read_metadata()
        if metadata_line:
            return metadata_line
        return None

    def _read_metadata(self):
        import select

        stderr_fd = self.proc.stderr.fileno()
        deadline = threading.Event()
        deadline.wait(timeout=5.0)

        while self.running and self.proc.poll() is None:
            ready, _, _ = select.select([stderr_fd], [], [], 0.5)
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
            chunk = self.proc.stdout.read(8192)
            if not chunk:
                break
            try:
                self.pcm_queue.put_nowait(chunk)
            except queue.Full:
                self.pcm_queue.get_nowait()
                self.pcm_queue.put_nowait(chunk)

    def read(self, min_bytes=8192):
        while self.running:
            if self._partial_buffer:
                chunk = self._partial_buffer
                self._partial_buffer = b""
            else:
                try:
                    chunk = self.pcm_queue.get(timeout=1.0)
                except queue.Empty:
                    return None

            if chunk is None:
                continue

            if len(chunk) >= min_bytes:
                result = chunk[:min_bytes]
                self._partial_buffer = chunk[min_bytes:]
                return result
            else:
                self._partial_buffer += chunk

        return None

    def stop(self):
        self.running = False
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()


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
        self.delay_samples = max(1, int(delay_ms * sample_rate / 1000))

        self.delay_buffer = np.zeros((self.delay_samples, 2))
        self.delay_write_pos = 0
        self.delay_read_pos = 0

        self.sos = butter(2, bass_cutoff, btype="low", fs=sample_rate, output="sos")
        self.zi = None

        self.full_output_device = full_output_device
        self.bass_output_device = bass_output_device
        self.mute = mute

        self.full_queue = queue.Queue(maxsize=8)
        self.bass_queue = queue.Queue(maxsize=8)
        self.running = False

        self._print_config(
            full_output_device, bass_output_device, bass_cutoff, delay_ms, sample_rate
        )

    def _device_name(self, device_id):
        try:
            return sd.query_devices(device_id)["name"]
        except Exception:
            return f"Device {device_id}"

    def _print_config(self, full, bass, cutoff, delay, rate):
        print(f"Full Output:    {self._device_name(full)}")
        print(f"Bass Output:    {self._device_name(bass)}")
        print(f"Bass Cutoff:    {cutoff} Hz")
        print(f"Sync Delay:     {delay} ms ({self.delay_samples} samples)")
        print(f"Sample Rate:    {rate} Hz")
        print(f"Mute Tapped:    {self.mute}")
        print()

    def process_chunk(self, audio):
        if self.zi is None:
            self.zi = np.zeros((2, 2, audio.shape[1]))

        bass = sosfilt(self.sos, audio, axis=0, zi=self.zi)[0]

        delayed_full = self.delay_buffer[self.delay_read_pos % self.delay_samples]
        self.delay_buffer[self.delay_write_pos % self.delay_samples] = audio
        self.delay_write_pos += 1
        self.delay_read_pos += 1

        return delayed_full, bass

    def full_output_thread(self):
        stream = sd.OutputStream(
            device=self.full_output_device,
            samplerate=self.sample_rate,
            channels=2,
            latency="low",
        )
        stream.start()

        try:
            while self.running:
                chunk = self.full_queue.get(timeout=0.1)
                if chunk is None:
                    break
                stream.write(chunk)
        finally:
            stream.stop()
            stream.close()

    def bass_output_thread(self):
        stream = sd.OutputStream(
            device=self.bass_output_device,
            samplerate=self.sample_rate,
            channels=2,
            latency="low",
        )
        stream.start()

        try:
            while self.running:
                chunk = self.bass_queue.get(timeout=0.1)
                if chunk is None:
                    break
                stream.write(chunk)
        finally:
            stream.stop()
            stream.close()

    def run(self):
        self.running = True

        capture = AudioteeCapture(sample_rate=self.sample_rate, mute=self.mute)
        print("Starting audiotee (Core Audio Taps)...")
        metadata = capture.start()
        if metadata:
            print(
                f"Capture: {metadata.get('sample_rate')}Hz, {metadata.get('channels_per_frame')}ch, {metadata.get('encoding')}"
            )
        print("Running. Press Ctrl+C to stop.\n")

        full_thread = threading.Thread(target=self.full_output_thread, daemon=True)
        bass_thread = threading.Thread(target=self.bass_output_thread, daemon=True)
        full_thread.start()
        bass_thread.start()

        bytes_per_sample = 4
        bytes_per_chunk = int(self.sample_rate * 0.02) * 2 * bytes_per_sample

        try:
            while self.running:
                raw = capture.read(bytes_per_chunk)
                if raw is None:
                    continue

                audio = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)

                delayed_full, bass = self.process_chunk(audio)

                try:
                    self.full_queue.put_nowait(delayed_full)
                except queue.Full:
                    self.full_queue.get_nowait()
                    self.full_queue.put_nowait(delayed_full)

                try:
                    self.bass_queue.put_nowait(bass)
                except queue.Full:
                    self.bass_queue.get_nowait()
                    self.bass_queue.put_nowait(bass)

        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            self.running = False
            try:
                self.full_queue.put_nowait(None)
            except Exception:
                pass
            try:
                self.bass_queue.put_nowait(None)
            except Exception:
                pass
            full_thread.join(timeout=2)
            bass_thread.join(timeout=2)
            capture.stop()


def list_devices():
    devices = sd.query_devices()
    print(f"{'ID':<4} {'Name':<30} {'In':<4} {'Out':<4} {'Rate':<6}")
    print("-" * 52)
    for i, d in enumerate(devices):
        name = d["name"][:28]
        rate = int(d["default_samplerate"]) if d["default_samplerate"] else "N/A"
        print(
            f"{i:<4} {name:<30} {d['max_input_channels']:<4} {d['max_output_channels']:<4} {rate:<6}"
        )
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Audio Router - Split system audio into full-range + bass-only outputs"
    )
    parser.add_argument(
        "--list", action="store_true", help="List available audio devices"
    )
    parser.add_argument("--full", type=int, help="Full-range output device ID")
    parser.add_argument(
        "--bass", type=int, help="Bass-only output device ID (WKing D8 Mini)"
    )
    parser.add_argument(
        "--cutoff",
        type=int,
        default=DEFAULT_BASS_CUTOFF,
        help="Bass cutoff frequency in Hz (default: 80)",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=DEFAULT_DELAY_MS,
        help="Delay in ms to sync Bluetooth latency (default: 150)",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help="Sample rate (default: 48000)",
    )
    parser.add_argument(
        "--no-mute",
        action="store_true",
        help="Don't mute the tapped audio (you'll hear it from original source)",
    )

    args = parser.parse_args()

    if args.list:
        list_devices()
        return

    if args.full is None or args.bass is None:
        parser.print_help()
        print("\nError: --full and --bass are required.")
        print("Use --list to see available devices.\n")
        sys.exit(1)

    router = AudioRouter(
        full_output_device=args.full,
        bass_output_device=args.bass,
        sample_rate=args.rate,
        bass_cutoff=args.cutoff,
        delay_ms=args.delay,
        mute=not args.no_mute,
    )
    router.run()


if __name__ == "__main__":
    main()

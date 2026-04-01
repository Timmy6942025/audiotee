# Audio Router

Split macOS system audio into multiple outputs with frequency filtering and latency compensation.

**Use case:** Route full-range audio to your MacBook speakers while sending bass-only (<80Hz) to a Bluetooth speaker — turning it into a wireless subwoofer, perfectly in sync.

## Features

- **Zero-sudo** — Uses Core Audio Taps API (macOS 14.2+). No kernel extensions, no drivers.
- **Real-time crossover** — Butterworth low-pass filter splits frequencies per output
- **Bluetooth sync** — Configurable delay buffer compensates for wireless latency
- **Lossless** — Raw PCM processing, no re-encoding

## Architecture

```
System Audio
    │
    ▼
┌─────────────┐
│  audiotee   │  Core Audio Taps API (user-space, no sudo)
│  (Swift)    │  Streams raw PCM to stdout
└──────┬──────┘
       │ 32-bit float, 48kHz, stereo
       ▼
┌─────────────────────────────────────────────┐
│            Audio Router (Python)            │
│                                             │
│  ┌──────────────┐    ┌──────────────────┐  │
│  │ Low-pass     │    │ Delay buffer     │  │
│  │ filter (<80Hz│    │ (sync comp.)     │  │
│  │ Butterworth) │    │                  │  │
│  └──────┬───────┘    └────────┬─────────┘  │
│         │                     │            │
│         ▼                     ▼            │
│   Bass Queue           Full Queue          │
└────┬────────┬──────────────┬───────────────┘
     │        │              │
     ▼        ▼              ▼
  Output 1  Output 2     (separate sounddevice streams)
  (Bass)    (Full-range)
```

## Requirements

- **macOS 14.2+** (Core Audio Taps API)
- **Python 3.10+**
- **Swift 5.9+** (to build audiotee)
- **Screen and System Audio Recording** permission for your terminal app

## Quick Start

### 1. Build audiotee

```bash
cd ~
git clone https://github.com/makeusabrew/audiotee.git
cd audiotee
swift build -c release
```

### 2. Install Python dependencies

```bash
pip3 install sounddevice numpy scipy
```

### 3. Find your output device IDs

```bash
python3 bin/audio_router.py --list
```

Example output:
```
ID   Name                           In   Out  Rate
----------------------------------------------------
0    MacBook Pro Microphone         1    0    44100
1    MacBook Pro Speakers           0    2    44100
2    WKing D8 Mini                  0    2    48000
3    Multi-Output Device            0    2    44100
```

### 4. Run the router

```bash
python3 bin/audio_router.py --full 1 --bass 2
```

This sends full-range audio to your MacBook speakers (device 1) and bass-only to your Bluetooth speaker (device 2).

## Configuration

| Flag | Default | Description |
|---|---|---|
| `--full <ID>` | *(required)* | Device ID for full-range output |
| `--bass <ID>` | *(required)* | Device ID for bass-only output |
| `--cutoff <Hz>` | `80` | Bass crossover frequency |
| `--delay <ms>` | `150` | Delay on full-range output to sync with Bluetooth |
| `--rate <Hz>` | `48000` | Sample rate |
| `--no-mute` | | Don't mute the tapped audio source |
| `--list` | | List available audio devices |

## Tuning

### Sync delay

Start with `--delay 150` and adjust by ear:
- **Hear echo/phase issues** → increase delay
- **Bass arrives late** → decrease delay
- Typical Bluetooth latency: 100–250ms

### Crossover frequency

- **80Hz** (default) — Standard subwoofer crossover
- **60Hz** — Tighter bass, less overlap
- **100Hz** — More bass on the external speaker

### Example: calibrated setup

```bash
python3 bin/audio_router.py --full 1 --bass 2 --cutoff 60 --delay 180
```

## How It Works

### System Audio Capture

macOS doesn't expose a "Stereo Mix" device. Traditional solutions (BlackHole, Soundflower, Loopback) install kernel extensions requiring sudo.

This tool uses Apple's **Core Audio Taps API** (introduced in macOS 14.2), which creates a user-space audio tap on system audio — no drivers, no root access needed. The tap streams raw PCM through [audiotee](https://github.com/makeusabrew/audiotee).

### Frequency Splitting

A 2nd-order Butterworth low-pass filter at the configured cutoff frequency extracts bass content. The full-range signal is the original minus the bass (complementary crossover), preserving total energy.

### Latency Compensation

Bluetooth speakers introduce 100–250ms of latency. A circular delay buffer holds the full-range signal, releasing it after the configured delay so both outputs arrive in sync.

## Troubleshooting

### "Permission denied" or no audio captured

Grant **Screen and System Audio Recording** permission:
1. System Settings → Privacy & Security → Screen and System Audio Recording
2. Add your terminal app (Terminal.app, iTerm2, VS Code, etc.)
3. Restart the terminal

### Audio crackling or dropouts

Increase the delay buffer or try a larger `--buffer` value. Bluetooth interference can cause intermittent dropouts.

### Bass sounds weak

Lower the crossover frequency (`--cutoff 60`) or increase your Bluetooth speaker's volume relative to the MacBook.

### Hear audio twice (echo)

The tapped audio is still playing from its original source. Use the default mute behavior, or ensure you're not routing through a Multi-Output Device that includes the original output.

## License

MIT

# dx7toFM1

**English** · [简体中文](README.zh-CN.md)

Split a Yamaha DX7 32-voice bank (`.syx`) into individual single-voice `.syx` files,
each named after the voice it contains, so FM1's third-party firmware can import them
one by one.

---

## Why this exists

FM1 plays DX7 voices, but its **third-party firmware only imports one voice per file** —
it has no support for banks. What actually circulates online — factory ROMs, cartridge
rips, patch collections — is almost always a **32-voice bank dump**, which the firmware
cannot use as-is:

| | DX7 32-voice bank | DX7 single voice |
|---|---|---|
| Header | `F0 43 0n 09 20 00` | `F0 43 0n 00 01 1B` |
| Payload | 4096 bytes — 128 per voice, **packed** | 155 bytes, **unpacked** |
| Total file size | 4104 bytes | 163 bytes |
| FM1 third-party firmware | ❌ cannot import | ✅ imports directly |

The two are not just different sizes: in a bank, each voice is bit-packed into 128
bytes, with several parameters sharing the same byte. A single-voice dump stores one
parameter per byte in 155 bytes. So a bank has to be *unpacked* and re-wrapped in a
proper single-voice SysEx message — which is exactly what this tool does, for all 32
voices in one go.

## Where to get DX7 patches

Original DX7 patch collections (ROM cartridges and more) can be downloaded here:

**<https://yamahablackboxes.com/collection/yamaha-dx7-synthesizer/patches/>**

Grab any `.syx` bank from that collection, run it through `dx7toFM1.py`, and you get a
folder of individually named voices.

## Requirements

Python 3.8 or newer. Standard library only — no `pip install` needed.

## Quick start

```bash
python3 dx7toFM1.py rom1a.syx
```

```
rom1a.syx — split: 32 voice(s)
  01  'BRASS   1'    -> BRASS 1.syx
  02  'BRASS   2'    -> BRASS 2.syx
  03  'BRASS   3'    -> BRASS 3.syx
  ...
  32  'TAKE OFF'     -> TAKE OFF.syx

Total: 32 voice(s).
```

Every file in `rom1a_voices/` is a valid 163-byte DX7 single-voice dump that FM1's
third-party firmware can import directly. The filename is the voice name stored inside the bank.

## Usage

```bash
python3 dx7toFM1.py <bank.syx> [<bank2.syx> ...] [options]
```

| Option | Description |
|---|---|
| `-o, --out DIR` | Output directory (default: `<input>_voices`) |
| `-n, --numbered` | Prefix filenames with a two-digit index: `01_BRASS 1.syx` |
| `-p, --prefix STR` | Add a fixed prefix to every filename |
| `-u, --underscore` | Turn spaces in voice names into `_`: `BRASS_1.syx` |
| `-l, --list` | Only list the voice names, write no files |
| `-q, --quiet` | Do not print the per-voice listing |
| `--selftest` | Run the pack/unpack round-trip self-test and exit |

Examples:

```bash
# default: ./rom1a_voices/BRASS 1.syx ...
python3 dx7toFM1.py rom1a.syx

# numbered + underscore names into a chosen folder
python3 dx7toFM1.py rom1a.syx -o voices -n -u

# batch-convert a whole folder of banks (each gets its own subfolder)
python3 dx7toFM1.py *.syx -o voices

# just look at what is inside a bank
python3 dx7toFM1.py rom1a.syx --list
```

## Output

Each written file is byte-for-byte a standard DX7 single-voice dump:

```
F0 43 0n 00 01 1B | 155 bytes of unpacked voice data | checksum | F7
```

- The checksum is the standard Yamaha 7-bit checksum, `(-sum) & 0x7F`.
- The device/channel number `0n` is copied from the source bank.
- The voice name is taken from the 10 name characters stored in the bank.

File naming rules:

- Trailing padding spaces are trimmed (`BRASS   1` → `BRASS 1`), inner spaces are kept.
- Characters that are illegal in filenames (`/ \ : * ? " < > |`) become `_`.
- Duplicate names are de-duplicated with a suffix: `BRASS 1.syx`, `BRASS 1-2.syx`, ….
- A voice with a blank name falls back to `voice_01`, `voice_02`, ….

## How it works

**Bank message** (4104 bytes):

```
F0 43 0n 09 20 00 | 4096 bytes | checksum | F7
                     32 voices × 128 bytes, packed
```

**Packed voice** (128 bytes) — 6 operators × 17 bytes, then globals, then the name:

| Bytes | Contents |
|---|---|
| 0–16 | OP6 (operator 6) |
| 17–33 | OP5 |
| 34–50 | OP4 |
| 51–67 | OP3 |
| 68–84 | OP2 |
| 85–101 | OP1 |
| 102–109 | Pitch EG rates 1–4, levels 1–4 |
| 110 | Algorithm (0–31) |
| 111 | bit 3 osc key sync, bits 2–0 feedback |
| 112–115 | LFO speed, delay, pitch mod depth, amp mod depth |
| 116 | bits 6–4 pitch mod sens, 3–1 LFO wave, 0 LFO sync |
| 117 | Transpose (0–48, 24 = centre) |
| 118–127 | Voice name, 10 ASCII characters |

**Packed operator** (17 bytes) — the crowded one, bit fields sharing a byte:

| Byte | Contents |
|---|---|
| 0–3 | EG rates 1–4 (0–99) |
| 4–7 | EG levels 1–4 (0–99) |
| 8 | Keyboard level scaling break point |
| 9 | Left depth |
| 10 | Right depth |
| 11 | bits 3–2 right curve, bits 1–0 left curve (0–3) |
| 12 | bits 6–3 detune (0–14), bits 2–0 rate scaling (0–7) |
| 13 | bits 4–2 key velocity sens (0–7), bits 1–0 amp mod sens (0–3) |
| 14 | Operator output level (0–99) |
| 15 | bits 5–1 frequency coarse (0–31), bit 0 oscillator mode |
| 16 | Frequency fine (0–99) |

**Unpacked single voice** (155 bytes) — one value per byte:

| Bytes | Contents |
|---|---|
| 0–20, 21–41, …, 105–125 | OP6 … OP1, 21 bytes each |
| 126–133 | Pitch EG rates 1–4, levels 1–4 |
| 134 | Algorithm |
| 135 | Feedback |
| 136 | Oscillator key sync |
| 137–140 | LFO speed, delay, pitch mod depth, amp mod depth |
| 141 | LFO sync |
| 142 | LFO waveform |
| 143 | Pitch mod sensitivity |
| 144 | Transpose |
| 145–154 | Voice name |

The tool implements both directions (`unpack_voice()` / `pack_voice()`) and uses the
inverse function as a self-check.

## Verification

- All 32 voices of `rom1a.syx`: `pack(unpack(voice))` reproduces the original 128 bytes
  byte-for-byte — no bits dropped or misplaced.
- Feeding `tine-ep.syx` (a known good single-voice dump) through the tool produces a file
  that is byte-identical to the original, checksum included.
- Voice names recovered from `rom1a.syx` match the official ROM1A voice list.
- `python3 dx7toFM1.py --selftest` runs 2000 randomised pack/unpack round-trips.

## Notes and limitations

- One-way only: it splits banks into single voices; it does not merge single voices back
  into a 32-voice bank.
- Input that is already a single voice is handled too — it gets re-wrapped and renamed
  rather than duplicated.
- A file containing several banks back to back is split completely.
- Damaged checksums are reported as warnings rather than aborting the conversion.

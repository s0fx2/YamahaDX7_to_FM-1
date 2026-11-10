#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dx7toFM1 1.1.0 — DX7 音色库转换工具
=====================================

FM1 的第三方固件只能逐个导入单个 DX7 音色（single voice dump, 163 字节），
不认 DX7 的 32 音色整合包（bank / bulk dump, 4104 字节）。

两种输出模式：
  1) 默认：把整合包拆成 32 个单音色 .syx 文件，文件名取自音色名，逐个导入；
  2) --json：生成 Felucca/Skiff 的 felucca-library v1 库文件，一次导入多个音色。
     该格式里 patch.fm6 就是 128 字节 DX7 打包音色，整块复制即可，无需 unpack。

格式依据 DX7 MIDI Data Format（Yamaha 官方数据表 / Dexed sysex-format.txt）：

  32 音色库消息：
      F0 43 0n 09 20 00 | 4096 字节数据 | 校验和 | F7     共 4104 字节
      每个音色 128 字节，**打包(packed)** 布局

  单音色消息：
      F0 43 0n 00 01 1B | 155 字节数据 | 校验和 | F7      共 163 字节
      155 字节，**非打包(unpacked)** 布局

打包 -> 非打包的位域映射见 unpack_voice()；pack_voice() 是其逆运算，
用于回环自检（pack(unpack(x)) == x 才算解析正确）。

校验和：数据字节之和取负后 & 0x7F。

用法：
    python3 dx7toFM1.py rom1a.syx                  # 输出到 ./rom1a_voices/
    python3 dx7toFM1.py rom1a.syx -o out -n        # 文件名加序号 01_xxx.syx
    python3 dx7toFM1.py rom1a.syx --json           # 导出 Felucca 库 JSON
    python3 dx7toFM1.py rom1a.syx --list           # 只列出音色名，不写文件
    python3 dx7toFM1.py --version / --selftest     # 版本号 / 位域回环自检
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from datetime import datetime, timezone
from typing import Tuple

__version__ = "1.1.0"

# ---------------------------------------------------------------- 常量

SYSEX_START = 0xF0
SYSEX_END = 0xF7
YAMAHA_ID = 0x43

FMT_SINGLE_VOICE = 0x00
FMT_BANK_32 = 0x09

OP_COUNT = 6
OP_PACKED = 17                       # 打包格式：每个算子 17 字节
OP_UNPACKED = 21                     # 非打包格式：每个算子 21 字节
VOICE_PACKED = 128
VOICE_UNPACKED = 155
BANK_VOICES = 32
BANK_DATA = VOICE_PACKED * BANK_VOICES   # 4096

HEADER_LEN = 6                       # F0 43 0n 格式 计数高 计数低

# 打包格式中每个字节真正承载数据的位（其余位为保留位，必须为 0）
_PACKED_MASK = [0x7F] * VOICE_PACKED
for _op in range(OP_COUNT):
    _b = _op * OP_PACKED
    _PACKED_MASK[_b + 11] = 0x0F     # 0 0 0 | RC(2) | LC(2)
    _PACKED_MASK[_b + 13] = 0x1F     # 0 0 | KVS(3) | AMS(2)
    _PACKED_MASK[_b + 15] = 0x3F     # 0 | FC(5) | M(1)
_PACKED_MASK[110] = 0x1F             # 0 0 | ALG(5)
_PACKED_MASK[111] = 0x0F             # 0 0 0 | OKS(1) | FB(3)
_PACKED_MASK[116] = 0x77             # LPMS(3) | LFW(3) | LKS(1)


# ---------------------------------------------------------------- 校验和

def yamaha_checksum(data: bytes) -> int:
    """(0x80 - (sum & 0x7F)) & 0x7F，等价于 (-sum) & 0x7F。"""
    return (-sum(data)) & 0x7F


# ---------------------------------------------------------------- 位域转换

def unpack_voice(packed: bytes) -> bytes:
    """128 字节打包音色 -> 155 字节非打包音色。"""
    if len(packed) != VOICE_PACKED:
        raise ValueError(f"packed voice must be {VOICE_PACKED} bytes, got {len(packed)}")

    u = bytearray(VOICE_UNPACKED)

    for op in range(OP_COUNT):          # 0=OP6 ... 5=OP1，两种格式顺序一致
        p = op * OP_PACKED
        o = op * OP_UNPACKED

        u[o + 0:o + 8] = packed[p + 0:p + 8]      # EG R1-R4, L1-L4
        u[o + 8] = packed[p + 8]                  # 键盘电平缩放 断点
        u[o + 9] = packed[p + 9]                  # 左侧深度
        u[o + 10] = packed[p + 10]                # 右侧深度

        b = packed[p + 11]                        # 0 0 0 | RC | LC
        u[o + 11] = b & 0x03                      # 左曲线
        u[o + 12] = (b >> 2) & 0x03               # 右曲线

        b = packed[p + 12]                        # DET(4) | RS(3)
        u[o + 13] = b & 0x07                      # 速率缩放
        u[o + 20] = (b >> 3) & 0x0F               # 失谐 0-14

        b = packed[p + 13]                        # 0 0 | KVS(3) | AMS(2)
        u[o + 14] = b & 0x03                      # 幅度调制灵敏度
        u[o + 15] = (b >> 2) & 0x07               # 键速灵敏度

        u[o + 16] = packed[p + 14]                # 算子输出电平

        b = packed[p + 15]                        # 0 | FC(5) | M(1)
        u[o + 17] = b & 0x01                      # 振荡器模式 0=ratio
        u[o + 18] = (b >> 1) & 0x1F               # 频率粗调

        u[o + 19] = packed[p + 16]                # 频率细调

    u[126:134] = packed[102:110]                  # 音高 EG R1-R4, L1-L4

    u[134] = packed[110] & 0x1F                   # 算法 0-31
    u[135] = packed[111] & 0x07                   # 反馈 0-7
    u[136] = (packed[111] >> 3) & 0x01            # 振荡器键同步
    u[137] = packed[112]                          # LFO 速度
    u[138] = packed[113]                          # LFO 延迟
    u[139] = packed[114]                          # LFO 音高调制深度
    u[140] = packed[115]                          # LFO 幅度调制深度

    b = packed[116]                               # LPMS(3) | LFW(3) | LKS(1)
    u[141] = b & 0x01                             # LFO 键同步
    u[142] = (b >> 1) & 0x07                      # LFO 波形 0-5
    u[143] = (b >> 4) & 0x07                      # 音高调制灵敏度

    u[144] = packed[117]                          # 移调 0-48（24=中央）

    u[145:155] = packed[118:128]                  # 音色名 10 字符 ASCII

    return bytes(u)


def pack_voice(unpacked: bytes) -> bytes:
    """155 字节非打包音色 -> 128 字节打包音色（unpack_voice 的逆运算）。"""
    if len(unpacked) != VOICE_UNPACKED:
        raise ValueError(f"unpacked voice must be {VOICE_UNPACKED} bytes, got {len(unpacked)}")

    p = bytearray(VOICE_PACKED)

    for op in range(OP_COUNT):
        o = op * OP_UNPACKED
        b = op * OP_PACKED

        p[b + 0:b + 8] = unpacked[o + 0:o + 8]
        p[b + 8] = unpacked[o + 8]
        p[b + 9] = unpacked[o + 9]
        p[b + 10] = unpacked[o + 10]
        p[b + 11] = (unpacked[o + 11] & 0x03) | ((unpacked[o + 12] & 0x03) << 2)
        p[b + 12] = (unpacked[o + 13] & 0x07) | ((unpacked[o + 20] & 0x0F) << 3)
        p[b + 13] = (unpacked[o + 14] & 0x03) | ((unpacked[o + 15] & 0x07) << 2)
        p[b + 14] = unpacked[o + 16]
        p[b + 15] = (unpacked[o + 17] & 0x01) | ((unpacked[o + 18] & 0x1F) << 1)
        p[b + 16] = unpacked[o + 19]

    p[102:110] = unpacked[126:134]
    p[110] = unpacked[134] & 0x1F
    p[111] = (unpacked[135] & 0x07) | ((unpacked[136] & 0x01) << 3)
    p[112] = unpacked[137]
    p[113] = unpacked[138]
    p[114] = unpacked[139]
    p[115] = unpacked[140]
    p[116] = ((unpacked[141] & 0x01)
              | ((unpacked[142] & 0x07) << 1)
              | ((unpacked[143] & 0x07) << 4))
    p[117] = unpacked[144]
    p[118:128] = unpacked[145:155]

    return bytes(p)


# ---------------------------------------------------------------- SysEx 构造/解析

def build_single_voice_dump(unpacked: bytes, device: int = 0) -> bytes:
    """把 155 字节非打包音色封装成 163 字节的单音色 SysEx 文件内容。"""
    if len(unpacked) != VOICE_UNPACKED:
        raise ValueError("data is not 155 bytes long")
    msg = bytearray()
    msg.append(SYSEX_START)
    msg.append(YAMAHA_ID)
    msg.append(device & 0x0F)
    msg.append(FMT_SINGLE_VOICE)
    msg.append((VOICE_UNPACKED >> 7) & 0x7F)     # 0x01
    msg.append(VOICE_UNPACKED & 0x7F)            # 0x1B
    msg += unpacked
    msg.append(yamaha_checksum(unpacked))
    msg.append(SYSEX_END)
    return bytes(msg)


def iter_sysex(data: bytes):
    """按 F0...F7 切分原始文件，返回 (起始偏移, 消息字节)。缺少 F7 时取到文件尾。"""
    i, n = 0, len(data)
    while i < n:
        if data[i] != SYSEX_START:
            i += 1
            continue
        j = data.find(bytes([SYSEX_END]), i + 1)
        if j < 0:
            yield i, data[i:]
            return
        yield i, data[i:j + 1]
        i = j + 1


def parse_message(msg: bytes) -> Tuple[str, int, bytes]:
    """返回 (类型, 设备号, 数据)。类型为 'bank' / 'voice' / 'other'。"""
    if len(msg) < HEADER_LEN + 2 or msg[0] != SYSEX_START or msg[1] != YAMAHA_ID:
        return "other", 0, b""
    body = msg[2:-1] if msg[-1] == SYSEX_END else msg[2:]
    if len(body) < 4:
        return "other", 0, b""
    device = body[0] & 0x0F
    fmt = body[1]
    count = (body[2] << 7) | body[3]
    payload = body[4:4 + count]
    if fmt == FMT_BANK_32 and len(payload) >= BANK_DATA:
        return "bank", device, payload[:BANK_DATA]
    if fmt == FMT_SINGLE_VOICE and len(payload) >= VOICE_UNPACKED:
        return "voice", device, payload[:VOICE_UNPACKED]
    return "other", device, payload


# ---------------------------------------------------------------- 文件名处理

_ILLEGAL = r'[\\/:*?"<>|\x00-\x1f]'


def voice_name(raw: bytes) -> str:
    """取 10 字节音色名，去掉尾部填充，解成可读字符串。"""
    s = raw.decode("ascii", errors="replace")
    s = s.replace("\x00", " ")
    # DX7 面板只有 ASCII，个别第三方音色库会把 0x7F 当占位符
    s = s.replace("\x7f", " ")
    s = re.sub(r"[\r\n\t]+", " ", s)
    return s.strip()


def safe_filename(name: str, spaces_as_underscore: bool = False) -> str:
    s = re.sub(_ILLEGAL, "_", name)
    s = re.sub(r"\s+", " ", s).strip()
    if spaces_as_underscore:
        s = s.replace(" ", "_")
    s = s.rstrip(" .")               # Windows 不允许结尾空格/点
    return s


def unique_path(directory: str, stem: str, used: set) -> str:
    """大小写不敏感去重（macOS/Windows 文件系统不分大小写）。"""
    candidate = stem
    n = 2
    while candidate.lower() in used:
        candidate = f"{stem}-{n}"
        n += 1
    used.add(candidate.lower())
    return os.path.join(directory, candidate + ".syx")


# ---------------------------------------------------------------- Felucca 库 JSON

# felucca-library v1 的内建模板，取自设备导出的 FELUCCA v2.5.1 布局。
# 固件升级后参数表会变（例如 v2.2 是 146/138，v2.5.1 是 148/140），
# 此时用 --template 指定设备新导出的库文件即可，无需改代码。
FELUCCA_FORMAT = "felucca-library"
FELUCCA_LIB_VERSION = 1
FELUCCA_FIRMWARE = "FELUCCA v2.5.1"
FELUCCA_PCOUNT = 148
FELUCCA_PE0 = 140
FELUCCA_ENGINE = 12
FELUCCA_ENGINE_NAME = "FM6"
FELUCCA_ENGINES = [
    "ANALOG", "-", "PHASE", "LOFI", "SAMPLE", "VOICE", "TRIO", "WHEEL",
    "GRAIN", "PHYS", "DRUM", "NOISE", "FM6", "SLICE", "DRUM-X",
]
FELUCCA_LABELS = (
    "LEVEL|ATTACK|DECAY|SUSTAIN|RELEASE|CUTOFF|PITCH|SHAPE|FX|RATE|WAVE|PHASE|FADE|"
    "PITCH#2|CUTOFF#2|SHAPE#2|AMP|MODE|RATE#2|OCTAVE|GATE|SWING|CHANCE|HOLD|ORDER|"
    "ROOT|SCALE|QUANTIZE|TRANSP|LENGTH|DIV|SWING#2|GATE#2|DIST|CHORUS|DELAY|REVERB|"
    "VOICE|GLIDE|PAN|MUTE|GL MODE|PRIORITY|ALLOC|DETUNE|SLICER|PATTERN|RATE#3|DEPTH|"
    "SRC1|DEST1|AMT1|SRC2|DEST2|AMT2|SRC3|DEST3|AMT3|SRC4|DEST4|AMT4|ATK|DEC|SUS|"
    "REL|LVL|ATK#2|DEC#2|SUS#2|REL#2|LVL#2|ATK#3|DEC#3|SUS#3|REL#3|LVL#3|ATK#4|"
    "DEC#4|SUS#4|REL#4|LVL#4|CHORD|VOICING|KICK|SNARE|CLAP|CL HAT|OP HAT|TOM|RIM|"
    "BELL|DIR|BY|DEPTH#2|RELEASE#2|TYPE|TONE|ATTACK#2|DECAY#2|SUSTAIN#2|RELEASE#3|"
    "TUNE|TUNE#2|TUNE#3|TUNE#4|TUNE#5|TUNE#6|TUNE#7|TUNE#8|DECAY#3|DECAY#4|DECAY#5|"
    "DECAY#6|DECAY#7|DECAY#8|DECAY#9|DECAY#10|SYNC|TRIG|POLARITY|SPREAD|TYPE#2|A|B|"
    "C|MIX|ENV SYNC|PAN#2|PAN#3|PAN#4|PAN#5|PAN#6|PAN#7|PAN#8|PAN#9|FOLLOW|MODE#2|"
    "NOTE|STRUM|DIR#2|E0|E1|E2|E3|E4|E5|E6|E7"
).split("|")

# params 的默认值，只记非零项；其余补 0。E0..E7（140..147）须保持 0，
# 它们是 FM6 的 ALGO/FEEDBACK/RATIO/PATCH 等宏，非零会改掉 DX7 原音色。
_FELUCCA_PARAM_DEFAULTS = {
    0: 110, 3: 127, 9: 60, 18: 2, 19: 1, 20: 64, 22: 127, 29: 16, 30: 2,
    32: 64, 34: 45, 35: 25, 36: 35, 44: 40, 46: 1, 47: 1, 48: 127, 63: 127,
    65: 127, 68: 127, 70: 127, 73: 127, 75: 127, 78: 127, 80: 127, 83: 127,
    84: 127, 85: 127, 86: 127, 87: 127, 88: 127, 89: 127, 90: 127, 93: 96,
    94: 73, 98: 60, 100: 40, 101: 64, 102: 64, 103: 64, 104: 64, 105: 64,
    106: 64, 107: 64, 108: 64, 122: 64, 123: 96, 124: 96, 125: 127,
}


def iso_now() -> str:
    """网页编辑器用的 UTC 时间戳格式，例如 2026-11-10T10:23:07.515Z。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def felucca_template(path: str = None) -> dict:
    """构造库 JSON 的模板；给了 path 就从设备导出的库文件读取布局。"""
    tmpl = {
        "format": FELUCCA_FORMAT,
        "version": FELUCCA_LIB_VERSION,
        "firmware": FELUCCA_FIRMWARE,
        "pCount": FELUCCA_PCOUNT,
        "pE0": FELUCCA_PE0,
        "paramLabels": list(FELUCCA_LABELS),
        "engines": list(FELUCCA_ENGINES),
        "engine": FELUCCA_ENGINE,
        "engineName": FELUCCA_ENGINE_NAME,
        "params": [_FELUCCA_PARAM_DEFAULTS.get(i, 0) for i in range(FELUCCA_PCOUNT)],
    }
    if not path:
        return tmpl

    with open(path, "r", encoding="utf-8") as fh:
        src = json.load(fh)
    if not isinstance(src, dict) or not isinstance(src.get("patches"), list):
        raise ValueError("template is not a felucca-library file")
    for key in ("format", "version", "firmware", "pCount", "pE0",
                "paramLabels", "engines"):
        if key in src:
            tmpl[key] = src[key]
    count = tmpl["pCount"]
    if len(tmpl["paramLabels"]) != count:
        raise ValueError(f"template: pCount {count} does not match "
                         f"paramLabels length {len(tmpl['paramLabels'])}")

    patches = [p for p in src["patches"] if isinstance(p, dict)]

    def fits(p):
        return (isinstance(p.get("params"), list)
                and len(p["params"]) == count
                and p.get("engineName") == "FM6")

    # 优先挑设备自己新建、参数全为具体数值的 FM6 patch；
    # 从旧布局重映射来的 patch 会在新增参数上留 null，退而求其次才用它。
    proto = next((p for p in patches
                  if fits(p) and all(v is not None for v in p["params"])), None)
    if proto is None:
        proto = next((p for p in patches if fits(p)), None)
    if proto is None:
        proto = next((p for p in patches
                      if isinstance(p.get("params"), list)
                      and len(p["params"]) == count), None)
    if proto is None:
        raise ValueError(f"template has no patch with {count} parameters")
    tmpl["params"] = list(proto["params"])
    tmpl["engine"] = proto.get("engine", tmpl["engine"])
    tmpl["engineName"] = proto.get("engineName", tmpl["engineName"])
    return tmpl


def collect_voices(path: str, verify: bool = True):
    """从 syx 中收集 (音色名, 128 字节打包音色)，同时返回警告列表。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    voices, warnings = [], []
    for offset, msg in iter_sysex(raw):
        kind, device, payload = parse_message(msg)
        if kind == "other":
            continue
        if verify and msg[-1] == SYSEX_END and len(msg) >= 8:
            body = msg[6:-2]
            if body and yamaha_checksum(body) != msg[-2]:
                warnings.append(
                    f"message at 0x{offset:X}: bad checksum (file may be damaged, continuing anyway)")
        if kind == "bank":
            chunks = [payload[i * VOICE_PACKED:(i + 1) * VOICE_PACKED]
                      for i in range(BANK_VOICES)]
        else:
            chunks = [pack_voice(payload)]
        for chunk in chunks:
            if verify and pack_voice(unpack_voice(chunk)) != chunk:
                warnings.append("voice %d: pack/unpack round-trip mismatch "
                                "(reserved bits set?)" % (len(voices) + 1))
            voices.append((voice_name(chunk[118:128]), bytes(chunk)))
    return voices, warnings


def build_library_json(voices, tmpl: dict, kind: str = "library") -> dict:
    """把打包音色列表组装成 felucca-library v1 文件（patch.fm6 原样复制）。"""
    now = iso_now()
    patches = []
    for index, (name, packed) in enumerate(voices):
        # 设备槽名上限 12 个可打印 ASCII；DX7 原名 10 字符，去掉补位空格即可
        label = re.sub(r"\s+", " ", name).strip()
        label = "".join(c for c in label if 32 <= ord(c) <= 126)[:12].strip()
        label = label or f"voice_{index + 1:02d}"
        patches.append({
            "name": label,
            "engine": tmpl["engine"],
            "engineName": tmpl["engineName"],
            "params": list(tmpl["params"]),
            "pattern": None,
            "tags": [],
            "created": now,
            "modified": now,
            "fm6": list(packed),
            "slot": index,
        })
    return {
        "format": tmpl["format"],
        "version": tmpl["version"],
        "kind": kind,
        "firmware": tmpl["firmware"],
        "pCount": tmpl["pCount"],
        "pE0": tmpl["pE0"],
        "paramLabels": list(tmpl["paramLabels"]),
        "engines": list(tmpl["engines"]),
        "exported": now,
        "patches": patches,
    }


# ---------------------------------------------------------------- 主流程

class Result:
    def __init__(self):
        self.written = []       # (序号, 原音色名, 输出路径)
        self.warnings = []
        self.voices = 0


def split_file(path: str, out_dir: str = None, numbered: bool = False,
               prefix: str = "", spaces_as_underscore: bool = False,
               dry_run: bool = False, verify: bool = True) -> Result:
    with open(path, "rb") as fh:
        raw = fh.read()

    res = Result()
    if not out_dir:
        base = os.path.splitext(os.path.basename(path))[0]
        out_dir = base + "_voices"

    used: set = set()
    index = 0

    for offset, msg in iter_sysex(raw):
        kind, device, payload = parse_message(msg)
        if kind == "other":
            continue

        # 整条消息的校验和
        if verify and msg[-1] == SYSEX_END and len(msg) >= 8:
            body = msg[6:-2]
            if body and yamaha_checksum(body) != msg[-2]:
                res.warnings.append(
                    f"message at 0x{offset:X}: bad checksum (file may be damaged, continuing anyway)")

        if kind == "voice":
            # 输入里本来就是单音色，直接改名另存
            groups = [("voice", device, payload)]
        else:
            groups = []
            for v in range(BANK_VOICES):
                groups.append(("bank", device,
                               payload[v * VOICE_PACKED:(v + 1) * VOICE_PACKED]))

        for kind2, dev, chunk in groups:
            index += 1
            if kind2 == "bank":
                if verify and pack_voice(unpack_voice(chunk)) != chunk:
                    res.warnings.append(
                        f"voice {index}: pack/unpack round-trip mismatch (reserved bits set?)")
                unpacked = unpack_voice(chunk)
                packed_for_name = chunk
            else:
                unpacked = chunk
                packed_for_name = pack_voice(chunk)

            name = voice_name(packed_for_name[118:128])
            stem = safe_filename(name, spaces_as_underscore) or f"voice_{index:02d}"
            if prefix:
                stem = prefix + stem
            if numbered:
                stem = f"{index:02d}_{stem}"

            blob = build_single_voice_dump(unpacked, dev)

            if dry_run:
                res.written.append((index, name, os.path.join(out_dir, stem + ".syx")))
            else:
                os.makedirs(out_dir, exist_ok=True)
                target = unique_path(out_dir, stem, used)
                with open(target, "wb") as fh:
                    fh.write(blob)
                res.written.append((index, name, target))
            res.voices += 1

    return res


def selftest(rounds: int = 2000) -> bool:
    """随机打包音色 -> unpack -> pack，必须完全还原（忽略保留位）。"""
    rng = random.Random(0xD17)
    for _ in range(rounds):
        packed = bytes(rng.randrange(0x80) for _ in range(VOICE_PACKED))
        packed = bytes(b & m for b, m in zip(packed, _PACKED_MASK))
        if pack_voice(unpack_voice(packed)) != packed:
            print("self-test FAILED: pack/unpack round-trip mismatch", file=sys.stderr)
            return False
    # 真实素材：把 unpacked 再打包，检查各参数是否落在合法区间
    print(f"self-test passed: {rounds} random voices survived pack/unpack round-trip")
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="dx7toFM1: split a DX7 32-voice bank into single-voice .syx files, "
                    "or export a Felucca/Skiff library JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n  python3 dx7toFM1.py rom1a.syx\n"
               "  python3 dx7toFM1.py rom1a.syx -o out -n --underscore\n"
               "  python3 dx7toFM1.py rom1a.syx --json\n"
               "  python3 dx7toFM1.py rom1a.syx --json bank.json --kind bank\n")
    ap.add_argument("inputs", nargs="*", help="input .syx bank file(s); several are allowed")
    ap.add_argument("-o", "--out", help="output directory (default: <input>_voices)")
    ap.add_argument("-n", "--numbered", action="store_true",
                    help="prefix each filename with a two-digit index, e.g. 01_BRASS 1.syx")
    ap.add_argument("-p", "--prefix", default="", help="fixed prefix for every filename")
    ap.add_argument("-u", "--underscore", action="store_true",
                    help="replace spaces in voice names with underscores")
    ap.add_argument("-j", "--json", nargs="?", const="", metavar="FILE",
                    help="export a Felucca/Skiff library JSON instead of .syx files; "
                         "FILE is optional (default: <input>-felucca.json)")
    ap.add_argument("--template", metavar="FILE",
                    help="use a library JSON exported by your device as the schema template")
    ap.add_argument("--kind", choices=("library", "bank"), default="library",
                    help="value of the JSON kind field (default: library)")
    ap.add_argument("-l", "--list", action="store_true", help="only list voice names, write nothing")
    ap.add_argument("-q", "--quiet", action="store_true", help="suppress the per-voice listing")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--selftest", action="store_true", help="run the pack/unpack self-test and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return 0 if selftest() else 1

    if not args.inputs:
        ap.print_help()
        return 2

    json_mode = args.json is not None
    if json_mode and args.json and len(args.inputs) > 1:
        print("--json FILE accepts only one input file", file=sys.stderr)
        return 2

    tmpl = None
    if json_mode:
        try:
            tmpl = felucca_template(args.template)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"template error: {exc}", file=sys.stderr)
            return 2

    total = 0
    for path in args.inputs:
        if not os.path.isfile(path):
            print(f"skipped (not found): {path}", file=sys.stderr)
            continue

        if json_mode:
            voices, warnings = collect_voices(path)
            stem = os.path.splitext(os.path.basename(path))[0]
            target = args.json or os.path.join(args.out or ".", f"{stem}-felucca.json")
            if not args.list:
                parent = os.path.dirname(os.path.abspath(target))
                os.makedirs(parent, exist_ok=True)
                with open(target, "w", encoding="utf-8") as fh:
                    json.dump(build_library_json(voices, tmpl, args.kind),
                              fh, ensure_ascii=False, indent=1)
                    fh.write("\n")
            total += len(voices)
            if not args.quiet:
                print(f"\n{path} — JSON: {len(voices)} voice(s) -> {target}")
                for idx, (name, _) in enumerate(voices, 1):
                    shown = repr(name) if name else "(unnamed)"
                    print(f"  {idx:02d}  {shown:<14} -> {os.path.basename(target)}")
            for w in warnings:
                print(f"  warning: {w}", file=sys.stderr)
            if not voices:
                print(f"  no DX7 voice data found in: {path}", file=sys.stderr)
            continue

        out_dir = args.out
        if out_dir and len(args.inputs) > 1:
            out_dir = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0])

        res = split_file(path, out_dir=out_dir, numbered=args.numbered,
                         prefix=args.prefix,
                         spaces_as_underscore=args.underscore,
                         dry_run=args.list)
        total += res.voices

        if not args.quiet:
            title = "voice list" if args.list else "split"
            print(f"\n{path} — {title}: {res.voices} voice(s)")
            for idx, name, target in res.written:
                shown = repr(name) if name else "(unnamed)"
                label = target if args.list else os.path.basename(target)
                print(f"  {idx:02d}  {shown:<14} -> {label}")
        for w in res.warnings:
            print(f"  warning: {w}", file=sys.stderr)
        if res.voices == 0:
            print(f"  no DX7 voice data found in: {path}", file=sys.stderr)

    if not args.quiet:
        suffix = " in JSON mode" if json_mode else ""
        print(f"\nTotal: {total} voice(s){suffix}.")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())

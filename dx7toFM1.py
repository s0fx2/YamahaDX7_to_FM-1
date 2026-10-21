#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dx7toFM1 — DX7 音色库拆分工具
==============================

FM1 的第三方固件只能逐个导入单个 DX7 音色（single voice dump, 163 字节），
不认 DX7 的 32 音色整合包（bank / bulk dump, 4104 字节）。本工具把整合包拆成 32 个
单音色文件，文件名直接使用库内部的音色名，拆完即可逐个导入。

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
    python3 dx7toFM1.py rom1a.syx --list           # 只列出音色名，不写文件
    python3 dx7toFM1.py --selftest                 # 位域回环自检
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
from typing import Tuple

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
        description="dx7toFM1: split a DX7 32-voice bank (.syx) into single-voice files, "
                    "named after each voice",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n  python3 dx7toFM1.py rom1a.syx\n"
               "  python3 dx7toFM1.py rom1a.syx -o out -n --underscore\n")
    ap.add_argument("inputs", nargs="*", help="input .syx bank file(s); several are allowed")
    ap.add_argument("-o", "--out", help="output directory (default: <input>_voices)")
    ap.add_argument("-n", "--numbered", action="store_true",
                    help="prefix each filename with a two-digit index, e.g. 01_BRASS 1.syx")
    ap.add_argument("-p", "--prefix", default="", help="fixed prefix for every filename")
    ap.add_argument("-u", "--underscore", action="store_true",
                    help="replace spaces in voice names with underscores")
    ap.add_argument("-l", "--list", action="store_true", help="only list voice names, write nothing")
    ap.add_argument("-q", "--quiet", action="store_true", help="suppress the per-voice listing")
    ap.add_argument("--selftest", action="store_true", help="run the pack/unpack self-test and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return 0 if selftest() else 1

    if not args.inputs:
        ap.print_help()
        return 2

    total = 0
    for path in args.inputs:
        if not os.path.isfile(path):
            print(f"skipped (not found): {path}", file=sys.stderr)
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
        print(f"\nTotal: {total} voice(s).")
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())

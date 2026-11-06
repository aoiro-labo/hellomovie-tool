#!/usr/bin/env python3
"""
recover_token.py - 失われた token をパッケージから復元する

作品ごとの token はカタログ (movies.json) に入っていますが、配信が終わると
カタログから作品が消え、token も手元から消えます。ZIP は残っているので、
暗号文の先頭と平文の構造 (Ogg Opus / PNG のヘッダ) が分かっていれば
token (= 鍵表の種) を総当たりで復元できます。

使い方
------
  音声ガイドの場合 (暗号名は "sound")
    python tools/recover_token.py --zip hellomovie_downloads/2144_voiceguide_v1.zip

  字幕の場合 (暗号名は PNG のファイル名)
    python tools/recover_token.py --zip hellomovie_downloads/1997_subtitles_v1.zip \
        --entry subtitles/png/1220.dat

  見つかった token を tokens.json に登録する
    python tools/recover_token.py --zip ... --save

所要時間: 2^32 走査するため CPU 10〜30 分程度 (numpy を使用します)。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import zipfile
import zlib
from pathlib import Path
from typing import Dict, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8")

# 平文が確実に分かっている位置 (Ogg Opus ヘッダ / PNG ヘッダ)
OGG_KNOWN: Dict[int, bytes] = {
    0: b"OggS",              # シグネチャ
    4: b"\x00\x02",          # バージョン + ヘッダタイプ (継続ページ)
    6: b"\x00" * 8,          # グラnourished位置 (先頭ページは 0)
    18: b"\x00" * 4,         # ページシークエンス (0)
    26: b"\x11",             # セグメント数 (OpusHead 1 + 16)
    27: b"\x34",             # 最初のセグメント長 (52)
    28: b"\x00" * 16,        # 残りのセグメント長 (OpusTags)
    44: b"OpusHead",
    52: b"\x01",             # OpusHead バージョン
    56: b"\x00\x80\xbb\x00",  # 入力サンプリングレート 48000
    62: b"\x00",             # マッピングファミリー
}

PNG_KNOWN: Dict[int, bytes] = {
    8: b"\x00\x00\x00\x0d",  # IHDR の長さ
    12: b"IHDR",
    26: b"\x00",             # 圧縮方式
    27: b"\x00",             # フィルタ方式
    28: b"\x00",             # インターレース
}

MUL = np_uint32 = None  # 実行時に設定


def build_known(kind: str) -> Dict[int, bytes]:
    return dict(OGG_KNOWN) if kind == "audio" else dict(PNG_KNOWN)


def cipher_name_for(entry: str, kind: str) -> str:
    """暗号化に使われた名前 (音声は "sound"、PNG は拡張子なしのファイル名)。"""
    if kind == "audio":
        return "sound"
    return Path(entry).stem


def make_key_table(seed: int, count: int = 2048) -> bytes:
    """ATCipher の鍵表 (hlmv-dl / hellomovie_decrypt と同じ実装)。"""
    state = seed & 0xFFFFFFFF
    states = []
    for index in range(1, 5):
        state = (1812433253 * (state ^ (state >> 30)) + index) & 0xFFFFFFFF
        states.append(state)
    key = bytearray()
    for _ in range(count):
        x, y, z, w = states
        temporary = (x ^ (x << 11)) & 0xFFFFFFFFFFFFFFFF
        value = (w ^ (w >> 19) ^ temporary ^ (temporary >> 8)) & 0xFFFFFFFFFFFFFFFF
        states = [y, z, w, value]
        key.append(int(1.0 + (float(value) * 253.0) * (2.0**-64)))
    return bytes(key)


def tokens_for_seed(seed: int, name: str) -> list:
    """seed から token の候補を列挙します (符号と下位1bit の2通り)。"""
    crc = zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF
    results = []
    for negative in (False, True):
        mixed = (seed ^ 0xF229) & 0xFFFFFFFF
        if negative:
            mixed &= 0x7FFFFFFF
        for low in (0, 1):
            x32 = ((mixed << 1) | low) & 0xFFFFFFFF
            token = crc ^ x32
            for candidate in (token, token - (1 << 32)):
                if candidate not in results:
                    results.append(candidate)
    return results


def seed_for_token(token: int, name: str) -> int:
    crc = zlib.crc32(name.encode("utf-8")) & 0xFFFFFFFF
    return ((((crc ^ token) & 0xFFFFFFFFFFFFFFFF) >> 1) ^ 0xF229) & 0xFFFFFFFF


def is_valid_ogg(data: bytes) -> bool:
    if len(data) < 27 or data[:4] != b"OggS":
        return False
    packet_start = 27 + data[26]
    return len(data) >= packet_start + 8 and data[packet_start:packet_start + 8] == b"OpusHead"


def is_valid_png(data: bytes) -> bool:
    if len(data) < 33:
        return False
    if data[12:16] != b"IHDR":
        return False
    expected = zlib.crc32(data[12:29]) & 0xFFFFFFFF
    return int.from_bytes(data[29:33], "big") == expected


def brute_force(cipher: bytes, known: Dict[int, bytes], name: str, chunk: int = 1 << 22) -> Optional[int]:
    """既知の平文から鍵表onso一致する seed を探します。"""
    try:
        import numpy as np
    except ImportError:
        sys.exit("このツールには numpy が必要です: pip install numpy")

    # 鍵表の位置と平文の対応 (平文が分かる位置だけを使う)
    positions = sorted(known.keys())
    expected_key = {}
    for pos in positions:
        for offset, byte in enumerate(known[pos]):
            index = pos + offset
            if index >= len(cipher):
                return None
            expected_key[index] = cipher[index] ^ byte
    if not expected_key:
        return None

    # 最も後方の既知位置の直後まで計算すればよい (それ以降は情報がない)
    last = max(expected_key)
    steps = last + 1
    # ただし鍵表の先頭 8 バイトは構造的に 01 並びで情報にならないため、
    # その範囲だけを使うフィルタは避免する。
    usable = [i for i in expected_key if i >= 8]
    if not usable:
        sys.exit("鍵表の先頭のみしか分からないため、この方法では復元できません。")
    filter_pos = min(usable)

    mul = np.uint32(1812433253)
    print(f"鍵表の位置 {filter_pos} 以降で判定 (計算 {steps} ステップ)", flush=True)

    import time
    start = time.time()
    total = 1 << 32
    candidates = []
    for base in range(0, total, chunk):
        seeds = np.arange(base, base + chunk, dtype=np.uint64)
        s = seeds.astype(np.uint32)
        s1 = mul * (s ^ (s >> np.uint32(30))) + np.uint32(1)
        s2 = mul * (s1 ^ (s1 >> np.uint32(30))) + np.uint32(2)
        s3 = mul * (s2 ^ (s2 >> np.uint32(30))) + np.uint32(3)
        s4 = mul * (s3 ^ (s3 >> np.uint32(30))) + np.uint32(4)

        states = [s1, s2, s3, s4]
        for _ in range(steps):
            x = states.pop(0)
            w = states[-1]
            t = x.astype(np.uint64) ^ (x.astype(np.uint64) << np.uint64(11))
            states.append(w ^ (w >> np.uint64(19)) ^ t ^ (t >> np.uint64(8)))

        value = states[-1]
        hi = value >> np.uint64(56)
        approx = (((hi * np.uint64(253)) >> np.uint64(8)) + np.uint64(1)).astype(np.uint8)
        target = expected_key[filter_pos]
        mask = (approx == np.uint8(target)) | (approx == np.uint8((target - 1) & 0xFF))
        cand = seeds[mask]
        if cand.size:
            candidates.extend(cand.tolist())
        if (base // chunk) % 256 == 0:
            done = base + chunk
            rate = done / max(time.time() - start, 1e-9) / 1e6
            print(f"  {done/1e6:7.0f}M / 4096M  {rate:5.1f}M/s", flush=True)

    print(f"走査完了 ({time.time()-start:.1f}s)  候補 {len(candidates)} 件", flush=True)

    for seed in candidates:
        key = make_key_table(seed, 64)
        if all(key[i] == b for i, b in expected_key.items()):
            print(f"鍵表が一致: seed = 0x{seed:08x}", flush=True)
            return seed
    return None


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description="失われた token をパッケージから復元する", formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--zip", required=True, help="対象の ZIP パッケージ")
    p.add_argument("--entry", default=None, help="暗号化されたファイル (既定: 自動で1つ選ぶ)")
    p.add_argument("--kind", choices=["auto", "audio", "subtitle"], default="auto")
    p.add_argument("--save", action="store_true", help="見つかった token を tokens.json に登録する")
    p.add_argument("--tokens-file", default=None, help="tokens.json のパス")
    args = p.parse_args(argv)

    with zipfile.ZipFile(args.zip) as zf:
        names = zf.namelist()
        entry = args.entry
        kind = args.kind
        if entry is None:
            audio = [n for n in names if n.endswith("data/sound.dat")]
            png = [n for n in names if n.endswith(".dat") and "/png/" in n]
            if kind == "audio" or (kind == "auto" and audio):
                if not audio:
                    sys.exit("音声ファイルが見つかりません (--entry で指定してください)。")
                entry, kind = audio[0], "audio"
            elif png:
                entry, kind = png[0], "subtitle"
            else:
                sys.exit("候補が見つかりません (--entry で指定してください)。")
        elif kind == "auto":
            kind = "audio" if entry.endswith("data/sound.dat") else "subtitle"

        cipher = zf.read(entry)

    name = cipher_name_for(entry, kind)
    known = build_known(kind)
    print(f"対象: {args.zip}")
    print(f"エントリ: {entry}  暗号名: {name}  種別: {kind}")

    seed = brute_force(cipher, known, name)
    if seed is None:
        print("一致する seed が見つかりませんでした。")
        return 1

    key = make_key_table(seed)
    plain = bytes(value ^ key[index % 2048] for index, value in enumerate(cipher))
    valid = is_valid_ogg(plain) if kind == "audio" else is_valid_png(plain)
    print(f"復号結果の検証: {'OK' if valid else 'NG'}")

    for token in tokens_for_seed(seed, name):
        check = make_key_table(seed_for_token(token, name))
        if check == key:
            print(f"token = {token}")
            if args.save:
                path = Path(args.tokens_file) if args.tokens_file else \
                    Path(__file__).resolve().parent.parent / "hellomovie_downloads" / "tokens.json"
                _save_token(path, token)
                print(f"{path} に登録しました。")
            break
    return 0


def _save_token(path: Path, token: int) -> None:
    data = {"version": 1, "titles": {}}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {"version": 1, "titles": {}}
    data.setdefault("titles", {})
    data["titles"]["recovered"] = {"token": token, "recovered_at": ""}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

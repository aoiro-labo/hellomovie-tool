#!/usr/bin/env python3
"""
audio_sync.py - 「今流れている映画音声」を認識して、音声ガイドを同期再生するミニアプリ

公式 HELLO! MOVIE アプリ的做法を、本人による実装の代わりに Python で踏み直したものです。

公式アプリの仕組み (逆アセンブルして調べた結果)
------------------------------------------------
* マイクで入力を取り、native の evxfp (Evixar FingerPrint) が
  「指紋フレーム (FingerPrintFrame)」と「インデックス指紋 (IndexFingerPrint)」を作る
* 作品ごとに同梱された db/packed.db (AES128 で暗号化された DB) と照合し、
  一致率 (calcMatchRate) が type2_threshold (57%) 以上なら
  「どの作品のどの位置にいるか」が確定する
* 確定後は guide の再生位置を継続的に更新 (updateTimeParameter) してドリフトを詰める

このツールの違い
----------------
* 指紋 DB を「手元の映画ファイルから自分で作る」ので、公式の暗号化された DB は不要
* 照合は帯エネルギーを量子化した列 (AudFPrint と同じ考え方) をビット演算で比較する
* 入力は (1) ファイル (検証用) (2) マイク (3) Windows のシステム音声ループバック

使い方
------
  # 1) 手元の映画から指紋 DB を作る
  python sync/audio_sync.py build --movie "E:\\...\\00001.m2ts" --out fp_movie.pkl

  # 2) 流れている音声を認識してガイドを同期再生する
  python sync/audio_sync.py watch --db fp_movie.pkl --guide sound.ogg
  # Netflix をブラウザで再生している場合はループバックを使う
  python sync/audio_sync.py watch --db fp_movie.pkl --guide sound.ogg --input loopback
"""

from __future__ import annotations

import argparse
import pickle
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

SR = 16000        # 解析用サンプリングレート
FRAME = 1024      # 64 ms の窓
HOP = 512         # 32 ms  hop
NBANDS = 32       # 対数帯の数
F_MIN = 200.0
F_MAX = 4000.0
QUANT_BITS = 4    # 帯エネルギーの量子化ビット数 (0..15)
# 判定の閾値。ファイルを直接読む場合は 2 でも十分ですが、
# システム音 (ループバック) はサウンドカード側の処理 (ダウンミックス / EQ /
# 空間効果など) を隔着するのでスペクトルが動き、距離が大きくなります。
# そのため閾値を緩くし、得票の集中で判定します。
MAX_DISTANCE = 12  # 何個の帯が違えば「一致」とみなさないか
MIN_VOTES = 5       # これ以下の候補は採用しない
MIN_HITS = 5        # ヒットした窓数の下限 (無音などはこれ以下)
MIN_MARGIN = 2.0     # 最得票が 2 位の何倍である必要があるか

# 16 進 1 桁あたりの popcount
POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


# ---------------------------------------------------------------- 音声読み込み

def load_audio(path: str, start: float = 0.0, duration: Optional[float] = None) -> np.ndarray:
    """ffmpeg で音声を 16kHz モノラル PCM として読み込みます。"""
    cmd = ["ffmpeg", "-v", "error"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<i2").astype(np.float32) / 32768.0


def load_audio_fast(path: str, stream: Optional[int] = None) -> np.ndarray:
    """音声トラックを丸ごとデコードします (LPCM なら最速)。"""
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    if stream is not None:
        cmd = ["ffmpeg", "-v", "error", "-i", path, "-map", f"0:{stream}",
               "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<i2").astype(np.float32) / 32768.0


# ---------------------------------------------------------------- 特徴量

def band_features(samples: np.ndarray) -> np.ndarray:
    """対数帯のエネルギー (フレーム数, NBANDS) を返します。"""
    if len(samples) < FRAME:
        return np.zeros((0, NBANDS), dtype=np.float32)
    count = 1 + (len(samples) - FRAME) // HOP
    idx = np.arange(count)[:, None] * HOP + np.arange(FRAME)[None, :]
    frames = samples[idx] * np.hanning(FRAME).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
    freqs = np.fft.rfftfreq(FRAME, 1.0 / SR)
    edges = np.geomspace(F_MIN, F_MAX, NBANDS + 1)
    bands = np.zeros((count, NBANDS), dtype=np.float32)
    for i in range(NBANDS):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        if mask.any():
            bands[:, i] = spec[:, mask].mean(axis=1)
    return np.log10(bands + 1e-6)


def quantize(bands: np.ndarray) -> np.ndarray:
    """帯エネルギーを 0..15 に量子化します (フレーム内の相対値に正規化)。"""
    if len(bands) == 0:
        return np.zeros((0, NBANDS), dtype=np.uint8)
    centred = bands - bands.mean(axis=1, keepdims=True)
    scaled = np.clip(centred * 3.0 + 8.0, 0, 15)
    return scaled.astype(np.uint8)


def pack_nibbles(values: np.ndarray) -> np.ndarray:
    """0..15 の列を 16bit ごとにまとめて uint64 の列にします。"""
    if len(values) == 0:
        return np.zeros((0, NBANDS // 16), dtype=np.uint64)
    words = np.zeros((len(values), NBANDS // 16), dtype=np.uint64)
    for w in range(NBANDS // 16):
        acc = np.zeros(len(values), dtype=np.uint64)
        for n in range(16):
            acc |= values[:, w * 16 + n].astype(np.uint64) << np.uint64(4 * n)
        words[:, w] = acc
    return words


def nibble_popcount(words: np.ndarray) -> np.ndarray:
    """uint64 の各 16bit 内の 4bit 単位で popcount を取ります。"""
    if words.size == 0:
        return np.zeros(words.shape, dtype=np.int16)
    raw = words.view(np.uint8).reshape(words.shape[0], words.shape[1], 8)
    counts = POPCOUNT[raw]
    low = counts & 0x0F
    high = counts >> 4
    stacked = np.stack([low, high], axis=-1).reshape(words.shape[0], words.shape[1], 16)
    return stacked.sum(axis=-1, dtype=np.int16)


# ---------------------------------------------------------------- 指紋 DB

def probe_duration(path: str) -> float:
    """FFmpeg で長さを取得します (秒)。"""
    import shutil

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return 0.0
    try:
        out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path], capture_output=True, check=True).stdout
        return float(out.decode().strip())
    except (subprocess.CalledProcessError, ValueError):
        return 0.0


def _chunk_bounds(total: float, chunk: float) -> list[tuple[float, float]]:
    """並列デコード用の区間。

    窓はフレーム格子 (HOP サンプル) に固定されているので、
    区間の開始位置も格子に揃えておかないと時間がずれます。
    """
    frame_seconds = HOP / SR
    bounds = []
    position = 0.0
    while position < total:
        start = round(position / frame_seconds) * frame_seconds
        end = min(position + chunk, total)
        bounds.append((start, end - start))
        position = end
    return bounds


def _features_of_chunk(movie: str, start: float, duration: float) -> tuple[int, np.ndarray]:
    """1 区間の帯特徴量を計算します (並列実行用)。"""
    samples = load_audio(movie, start=start, duration=duration)
    features = band_features(samples)
    return int(round(start * SR / HOP)), features


def build_db(movie: str, out: str, start: float = 0.0, duration: Optional[float] = None,
             chunk: float = 600.0, workers: int = 4, progress=None) -> None:
    """映画音声から指紋 DB を作ります (区間を並列デコード)。"""
    import concurrent.futures

    print(f"映画音声を解析しています: {movie}", flush=True)
    total = duration if duration else probe_duration(movie)
    bounds = _chunk_bounds(max(total - start, 0.0), chunk)
    print(f"  {total:.1f} 秒を {len(bounds)} 区間に分けて {workers} 並列で解析します", flush=True)

    parts: list[tuple[int, np.ndarray]] = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_features_of_chunk, movie, start + s, d) for s, d in bounds]
        for future in concurrent.futures.as_completed(futures):
            offset, features = future.result()
            parts.append((offset, features))
            done += 1
            if progress is not None:
                progress(done, len(bounds))
            print(f"  {done}/{len(bounds)} 区間完了", flush=True)

    # 区間の順序を戻して連結する (フレームは独立なので境界の重複はない)
    parts.sort(key=lambda item: item[0])
    features = np.concatenate([item[1] for item in parts if len(item[1])], axis=0) if parts else \
        np.zeros((0, NBANDS), dtype=np.float32)
    packed = pack_nibbles(quantize(features))
    payload = {"version": 2, "sr": SR, "frame": FRAME, "hop": HOP, "nbands": NBANDS,
               "start": start, "total": total, "packed": packed}
    Path(out).write_bytes(pickle.dumps(payload, protocol=4))
    print(f"完成: {out} ({len(packed):,} 窓, {Path(out).stat().st_size/1024/1024:.1f} MB)", flush=True)


def match_position(db: np.ndarray, live: np.ndarray) -> Tuple[Optional[float], int, int]:
    """ライブ指紋から映画内の位置 (秒) を推定します。

    Returns:
        (位置の秒, 得票数, ヒットした窓数)
    """
    if len(db) == 0 or len(live) == 0:
        return None, 0, 0

    votes: dict = {}
    hits = 0
    # ライブ 1 窓ずつ DB 全体と突き合わせる (メモリを抑えるため 1 窓単位)
    for i in range(len(live)):
        xor = np.bitwise_xor(db, live[i][None, :])
        distance = nibble_popcount(xor).sum(axis=1)
        matched = np.flatnonzero(distance <= MAX_DISTANCE)
        if matched.size == 0:
            continue
        hits += 1
        for j in matched.tolist():
            offset = int(round((j - i) * HOP / SR * 2))  # 0.5 秒単位で binning
            votes[offset] = votes.get(offset, 0) + 1

    if not votes:
        return None, 0, hits
    best_offset, best_votes = max(votes.items(), key=lambda kv: kv[1])
    others = [count for key, count in votes.items() if key != best_offset]
    second = max(others) if others else 0
    if best_votes < MIN_MARGIN * max(second, 1):
        # 最得票が 2 位を分離できていないので採用しない
        return None, best_votes, hits
    return best_offset / 2.0, best_votes, hits


# ---------------------------------------------------------------- 入力

class AudioSource:
    """音声入力 (ファイル / マイク / システム音声ループバック)。"""

    def __init__(self, kind: str, path: Optional[str] = None, window: float = 6.0):
        self.kind = kind
        self.path = path
        self.window = window
        self.sr = SR

    def read(self) -> np.ndarray:
        if self.kind == "file":
            if self.path is None:
                raise SystemExit("--input file にはファイルが必要です。")
            return load_audio(self.path)
        return self._read_live()

    def _read_live(self) -> np.ndarray:
        """マイクまたはシステム音を取り込みます。

        Windows のシステム音は WASAPI の loopback で取得します
        (soundcard を使います。sounddevice の WASAPI は loopback に対応していません)。
        """
        try:
            import soundcard as sc
        except ImportError:
            raise SystemExit("マイク/ループバック入力には soundcard が必要です: pip install soundcard")

        if self.kind == "mic":
            device = sc.default_microphone()
            label = "マイク"
        else:  # loopback: 既定の出力デバイスの音をそのまま拾う
            speaker = sc.default_speaker()
            device = sc.get_microphone(speaker.id, include_loopback=True)
            label = f"システム音 ({speaker.name})"

        frames = int(self.window * self.sr)
        print(f"音声入力中 ({label})... Ctrl+C で終了")
        with device.recorder(samplerate=self.sr, channels=1) as recorder:
            data = recorder.record(numframes=frames)
        return np.asarray(data, dtype=np.float32).reshape(-1)


# ---------------------------------------------------------------- ガイド再生

def play_guide_from(guide: str, position: float, player: str = "mpv") -> Optional[subprocess.Popen]:
    """ガイドを指定位置から再生します。"""
    import shutil
    mpv = shutil.which("mpv")
    if mpv is None:
        for candidate in (r"C:\Program Files\MPV Player\mpv.exe", r"C:\Program Files\mpv\mpv.exe"):
            if Path(candidate).exists():
                mpv = candidate
                break
    if player == "mpv" and mpv is not None:
        return subprocess.Popen([
            mpv, "--no-terminal", "--no-config", "--audio-display=no", "--no-video",
            f"--start={max(position, 0.0):.3f}", guide,
        ])
    ffplay = shutil.which("ffplay")
    if ffplay is None:
        raise SystemExit("mpv も ffplay も見つかりません。")
    cmd = ["ffplay", "-nodisp", "-autoexit"]
    if position > 0:
        cmd += ["-ss", f"{position:.3f}"]
    cmd.append(guide)
    return subprocess.Popen(cmd)


# ---------------------------------------------------------------- コマンド

def cmd_build(args) -> int:
    build_db(args.movie, args.out, start=args.start, duration=args.duration)
    return 0


def cmd_watch(args) -> int:
    payload = pickle.loads(Path(args.db).read_bytes())
    db = payload["packed"]
    base = payload.get("start", 0.0)
    print(f"指紋 DB: {args.db} ({len(db):,} 窓, 映画長 {payload['total']:.1f} 秒, 開始 {base:.1f} 秒)")

    source = AudioSource(args.input, args.input_file, args.window)
    guide_position = None
    proc = None

    try:
        while True:
            samples = source.read()
            if len(samples) < FRAME * 2:
                time.sleep(0.2)
                continue
            live = pack_nibbles(quantize(band_features(samples)))
            position, votes, hits = match_position(db, live)
            if position is None or votes < MIN_VOTES or hits < MIN_HITS:
                print(f"  認識中... (得票 {votes} / ヒット {hits})", end="\r", flush=True)
                time.sleep(0.3)
                continue

            position += base
            print(f"\n認識: 映画内の {position:8.1f} 秒 (得票 {votes} / ヒット {hits})")
            target = position + args.guide_start
            if guide_position is None or abs(target - guide_position) > 1.0:
                if proc is None:
                    proc = play_guide_from(args.guide, target, args.player)
                    print(f"  ガイドを {target:.1f} 秒から再生します")
                else:
                    print(f"  ガイド位置を {target:.1f} 秒に更新します")
                guide_position = target
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n終了します。")
    finally:
        if proc is not None:
            proc.terminate()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="流れている映画音声を認識して音声ガイドを同期再生する",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="映画ファイルから指紋 DB を作る")
    b.add_argument("--movie", required=True)
    b.add_argument("--out", default="fp_movie.pkl")
    b.add_argument("--start", type=float, default=0.0, help="解析開始位置 (秒)")
    b.add_argument("--duration", type=float, default=None, help="解析する長さ (秒)")
    b.set_defaults(func=cmd_build)

    w = sub.add_parser("watch", help="音声入力を認識してガイドを同期再生する")
    w.add_argument("--db", required=True)
    w.add_argument("--guide", required=True)
    w.add_argument("--input", choices=["file", "mic", "loopback"], default="mic")
    w.add_argument("--input-file", default=None, help="--input file の対象")
    w.add_argument("--guide-start", type=float, default=0.0,
                   help="ガイドの t=0 が映画の何秒目に対応するか (BD: 0 / Netflix: 10)")
    w.add_argument("--window", type=float, default=6.0, help="認識に使う窓の長さ (秒)")
    w.add_argument("--interval", type=float, default=2.0, help="認識する間隔 (秒)")
    w.add_argument("--player", choices=["mpv", "ffplay"], default="mpv")
    w.set_defaults(func=cmd_watch)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

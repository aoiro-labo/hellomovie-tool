#!/usr/bin/env python3
"""
hellomovie_sync.py - HELLO! MOVIE 音声ガイドを Blu-ray / Netflix の映像に同期させるツール

アプリ本体はマイクで映画音声を指紋照合して同期しますが (packed.db / packed2.db、
EVXAudioManagerCPP.updateFPRecognizer)、ガイド音声はコメンタリーのみで映画音声と
相関しないため、ローカル再生では「時間ベース」の同期で十分です。

使い方
------
  1) 2つの映像ファイルのオフセットを測定する
     python hellomovie_sync.py scan --reference Netflix.mkv --target BD.m2ts

  2) 映像とガイドを同時に再生する (推奨)
     python hellomovie_sync.py play --movie BD.m2ts --guide sound.ogg
     python hellomovie_sync.py play --guide sound.ogg --guide-start 10 --countdown 3

  3) Netflix ブラウザ用に、ガイドの先頭を調整したファイルを作る
     python hellomovie_sync.py export --guide sound.ogg --guide-start 10 --output nf_guide.ogg

  4) 映像ファイルにガイドを埋め込む (容量elas尾声; 小さいファイル向け)
     python hellomovie_sync.py mux --movie movie.mkv --guide sound.ogg --output out.mkv

ガイドと映像の対応
------------------
ガイドの t=0 は本編の t=0 に相当します (冒頭 25 秒は無音リーダー)。
--guide-start は「映像の 0:00 の瞬間に、ガイドの何秒目を再生すべきか」です。
  * Blu-ray (BD):    BD = Netflix + 10s のため --guide-start 0
  * Netflix (配信):  先頭 10 秒が BD より短いため --guide-start 10
    (Netflix の 0:00 に ガイドの 10 秒目 が来るよう、先頭 10 秒をカットする)
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

AR = 8000  # 解析用のダウンサンプルレート


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, **kw)


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe is None:
        sys.exit("ffmpeg が見つかりません。FFmpeg をインストールするか PATH に追加してください。")
    return exe


def find_ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if exe is None:
        sys.exit("ffprobe が見つかりません。")
    return exe


# Windows で mpv をインストールした先去 (PATH に入っていないことがある)
MPV_FALLBACK_PATHS = [
    r"C:\Program Files\MPV Player\mpv.exe",
    r"C:\Program Files\mpv\mpv.exe",
    r"C:\Program Files (x86)\mpv\mpv.exe",
]


def find_mpv() -> Optional[str]:
    """mpv の実行ファイルを探します (PATH に無い場合は既知のインストール先も)。"""
    exe = shutil.which("mpv")
    if exe is not None:
        return exe
    for candidate in MPV_FALLBACK_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def pick_audio_stream(path: str, prefer_ja: bool = True) -> int:
    """日本語音声を優先してストリーム番号を選ぶ (見つからなければ最初の音声)。"""
    ffprobe = find_ffprobe()
    info = json.loads(
        _run([ffprobe, "-v", "error", "-show_entries", "stream=index,codec_type:stream_tags=language", "-of", "json", path]).stdout
    )
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        sys.exit(f"音声ストリームが見つかりません: {path}")
    if prefer_ja:
        for s in audio:
            lang = (s.get("tags") or {}).get("language", "")
            if lang and lang.lower().startswith("ja"):
                return s["index"]
    return audio[0]["index"]


# ---------------------------------------------------------------- scan

def _pcm(path: str, start: float, dur: float, stream: int, ar: int = AR):
    ffmpeg = find_ffmpeg()
    cmd = [ffmpeg, "-v", "error", "-ss", str(start), "-i", path,
           "-map", f"0:{stream}", "-t", str(dur), "-ac", "1", "-ar", str(ar), "-f", "s16le", "-"]
    out = _run(cmd, check=True).stdout
    if len(out) < 2:
        return []
    import numpy as np
    return np.frombuffer(out, dtype="<i2").astype(np.float32) / 32768.0


def _features(x, n_fft: int = 2048, hop: int = 400, bands: int = 48, ar: int = AR):
    import numpy as np
    n = 1 + (len(x) - n_fft) // hop
    if n <= 1:
        return None
    idx = np.arange(n)[:, None] * hop + np.arange(n_fft)[None, :]
    frames = x[idx] * np.hanning(n_fft).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)
    freqs = np.fft.rfftfreq(n_fft, 1 / ar)
    edges = np.geomspace(80, 3800, bands + 1)
    out = np.zeros((n, bands), dtype=np.float32)
    for i in range(bands):
        mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
        if mask.any():
            out[:, i] = spec[:, mask].mean(axis=1)
    out = np.log1p(out)
    out -= out.mean(axis=0, keepdims=True)
    out /= out.std(axis=0, keepdims=True) + 1e-6
    return out


def _align(a, b, max_lag_s: float, hop_s: float = 0.05):
    """a の各時刻が b の何秒遅れ/進みに対応するか (正 = a が遅れている) を推定する。"""
    import numpy as np
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    score = np.zeros(n)
    for f in range(a.shape[1]):
        score += np.fft.irfft(np.fft.rfft(a[:, f], n) * np.conj(np.fft.rfft(b[:, f], n)), n)
    lags = np.arange(n)
    lags = np.where(lags >= n // 2, lags - n, lags)
    mask = np.abs(lags) * hop_s <= max_lag_s
    ls = lags[mask] * hop_s
    sc = score[mask]
    i = int(np.argmax(sc))
    return float(ls[i]), float(sc[i] / len(a))


def cmd_scan(args) -> int:
    try:
        import numpy as np  # noqa: F401
    except ImportError:
        sys.exit("scan には numpy が必要です: pip install numpy")

    ref_stream = args.ref_stream if args.ref_stream is not None else pick_audio_stream(args.reference)
    tgt_stream = args.tgt_stream if args.tgt_stream is not None else pick_audio_stream(args.target)
    print(f"reference stream: 0:{ref_stream}  target stream: 0:{tgt_stream}")

    ref = _pcm(args.reference, args.start, args.duration, ref_stream)
    tgt = _pcm(args.target, args.start, args.duration, tgt_stream)
    a, b = _features(ref), _features(tgt)
    if a is None or b is None or len(a) < 100 or len(b) < 100:
        sys.exit("解析できるだけの音声がありません。--duration を増やしてください。")

    lag, score = _align(a, b, args.max_lag)
    # lag は reference が target より何秒遅れているか。offset は「同じ映像が
    # target では reference より何秒遅れて現れるか」(target - reference) と定義する。
    offset = -lag
    print()
    print(f"offset   : {offset:+.2f}s   (同じ映像が target では reference より {offset:+.2f}s 遅れて現れる)")
    print(f"score    : {score:.1f} / 48.0")
    print()
    print("例: 超かぐや姫！ では BD に対して offset=+10s です (BD = Netflix + 10s)。")
    if score < 10:
        print("スコアが低いです。このガイドはコメンタリーのみで映画音声と相関しないため、")
        print("scan は映画同士 (Blu-ray vs Netflix など) に対して使ってください。")
    elif score < 25:
        print("スコアが中程度です。--duration を長くするか、別の --start 位置でも確認してください。")
    else:
        print("スコアは十分です。このオフセットをそのまま mux / export の参考にできます。")
    return 0


# ---------------------------------------------------------------- guide 準備

def _prepare_guide(guide: str, guide_start: float, out: str) -> str:
    """ガイドの先頭を guide_start 秒カットし、トリム精度を上げるため Opus に再エンコードする。

    guide_start が 0 なら元ファイルをそのまま使う。
    """
    if guide_start <= 0:
        return guide
    ffmpeg = find_ffmpeg()
    cmd = [ffmpeg, "-v", "error", "-ss", f"{guide_start:.3f}", "-i", guide,
           "-vn", "-c:a", "libopus", "-b:a", "96k", "-f", "opus", out]
    _run(cmd, check=True)
    return out


def find_guide_for_title(title_id: int) -> Optional[Path]:
    base = Path(__file__).resolve().parent.parent / "hellomovie_downloads" / "decrypted"
    if not base.exists():
        return None
    cands = sorted(base.glob(f"{title_id}_voiceguide_v*/voiceguide/data/sound.ogg"))
    return cands[0] if cands else None


def resolve_guide(args) -> str:
    if not args.guide and not args.title_id:
        sys.exit("--guide か --title-id のどちらかが必要です。")
    guide = args.guide or find_guide_for_title(args.title_id)
    if guide is None:
        sys.exit(f"title_id {args.title_id} のガイドが見つかりませんでした。--guide で直接指定してください。")
    guide = str(guide)
    if not Path(guide).exists():
        sys.exit(f"ガイドがありません: {guide}")
    return guide


# ---------------------------------------------------------------- 同時再生

# 再生中に選べる较正値 (Lua スクリプトが guide-offset プロパティに書き込む)
OFFSETS_FILE_NAME = "sync_offsets.json"


class MpvClient:
    """mpv の IPC (JSON over 名前付きパイプ / Unix ソケット) を操作する小さなクライアント。"""

    def __init__(self, proc: subprocess.Popen, stream):
        self.proc = proc
        self.stream = stream
        self.buf = b""

    def command(self, cmd: list[Any]) -> Any:
        payload = json.dumps({"command": cmd}) + "\n"
        self.stream.sendall(payload.encode("utf-8"))
        return self._read()

    def set_property(self, name: str, value: Any) -> Any:
        return self.command(["set_property", name, value])

    def get_property(self, name: str) -> Any:
        response = self.command(["get_property", name])
        if isinstance(response, dict) and "data" in response:
            return response["data"]
        return None

    def _read(self) -> Any:
        # mpv は応答のほかにイベント (client-message など) も送ってくるので、
        # 応答 (request_id を持つもの) が出るまで読み飛ばす。
        while True:
            while b"\n" not in self.buf:
                chunk = self.stream.recv(65536)
                if not chunk:
                    return None
                self.buf += chunk
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                message = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if isinstance(message, dict) and "event" in message:
                continue
            return message

    def close(self) -> None:
        # "quit" の応答は mpv が終了する Ning 返ってこないので、待たずに terminate する
        try:
            self.stream.sendall(b'{"command":["quit"]}\n')
        except OSError:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass
        try:
            self.stream.close()
        except OSError:
            pass


class _PipeStream:
    """Windows の名前付きパイプを mpv の IPC と同じ interface で扱う薄いラッパ。"""

    def __init__(self, path: str):
        self.file = open(path, "r+b", buffering=0)

    def sendall(self, data: bytes) -> None:
        self.file.write(data)
        self.file.flush()

    def recv(self, size: int) -> bytes:
        return self.file.read(size) or b""

    def close(self) -> None:
        self.file.close()


_IPC_COUNTER = itertools.count()


def _ipc_address(tag: str) -> str:
    """mpv の IPC サーバアドレスを返します。

    Windows では名前付きパイプ、Unix ではソケットファイルを使います
    (mpv の --input-ipc-server は Windows では名前付きパイプのみ対応)。
    """
    unique = f"hmv-{tag}-{os.getpid()}-{next(_IPC_COUNTER)}"
    if os.name == "nt":
        return rf"\\.\pipe\{unique}"
    return f"/tmp/{unique}.sock"


def _connect_ipc(address: str, proc: subprocess.Popen) -> Any:
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if os.name == "nt":
                return _PipeStream(address)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(address)
            return sock
        except OSError:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
    raise RuntimeError(f"mpv の IPC に接続できませんでした: {address}")


def _start_mpv(tag: str, media: str, extra: list[str]) -> Optional[MpvClient]:
    mpv = find_mpv()
    if mpv is None:
        return None
    address = _ipc_address(tag)
    cmd = [mpv, "--no-terminal", "--no-config", "--idle=yes",
           f"--input-ipc-server={address}", media] + extra
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        stream = _connect_ipc(address, proc)
    except RuntimeError:
        proc.terminate()
        return None
    return MpvClient(proc, stream)


def _offsets_path(args) -> Path:
    if args.offsets_file:
        return Path(args.offsets_file)
    base = Path(__file__).resolve().parent.parent / "hellomovie_downloads"
    base.mkdir(parents=True, exist_ok=True)
    return base / OFFSETS_FILE_NAME


def _load_offsets(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_offset(path: Path, key: str, value: float) -> None:
    data = _load_offsets(path)
    data[key] = round(value, 3)
    try:
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def _movie_key(movie: str) -> str:
    return os.path.normcase(os.path.abspath(movie))


def _play_with_ffplay(movie: Optional[str], guide: str, guide_start: float) -> int:
    """mpv が無いときの簡易再生 (位置取得ができないため自動補正はできない)。"""
    ffplay = shutil.which("ffplay")
    if ffplay is None:
        sys.exit("mpv も ffplay も見つかりません。mpv (https://mpv.io) をインストールしてください。")
    procs = []
    if movie:
        procs.append(subprocess.Popen([ffplay, "-autoexit", movie]))
    guide_cmd = [ffplay, "-nodisp", "-autoexit"]
    if guide_start > 0:
        guide_cmd += ["-ss", f"{guide_start:.3f}"]
    guide_cmd.append(guide)
    procs.append(subprocess.Popen(guide_cmd))
    print("ffplay で再生します。ガイドの位置は自動補正できないため、開始時刻を合わせてください。")
    for p in procs:
        p.wait()
    return 0


def cmd_play(args) -> int:
    guide = resolve_guide(args)
    offsets_path = _offsets_path(args)

    guide_start = args.guide_start
    if args.movie:
        key = _movie_key(args.movie)
        saved = _load_offsets(offsets_path).get(key)
        if saved is not None and args.auto:
            guide_start += saved
            print(f"保存済みの補正値 {saved:+.2f}s を適用します (合計 {guide_start:.2f}s)")
        elif saved is not None:
            print(f"保存済みの補正値: {saved:+.2f}s (--auto で自動適用)")

    if args.countdown > 0:
        for i in range(args.countdown, 0, -1):
            print(f"  {i} ...", flush=True)
            time.sleep(1.0)

    movie_client = None
    guide_client = None
    offset_file = Path(tempfile.gettempdir()) / f"hmv-guide-offset-{os.getpid()}.txt"
    offset_file.write_text("0.000", encoding="utf-8")
    if args.movie:
        lua = Path(__file__).resolve().parent / "guide_nudge.lua"
        extra = ["--pause=yes"]
        if args.no_video_output:
            extra.append("--vo=null")
        if lua.exists():
            extra.append(f"--script={lua}")
            extra.append(f"--script-opts=guide_nudge-offset_file={offset_file}")
        movie_client = _start_mpv("movie", args.movie, extra)
    guide_client = _start_mpv("guide", guide, [
        "--pause=yes", "--audio-display=no", "--no-video",
        f"--start={guide_start:.3f}", "--keep-open=no",
    ])

    if guide_client is None:
        if movie_client is not None:
            movie_client.close()
        return _play_with_ffplay(args.movie, guide, guide_start)

    if movie_client is None:
        sys.exit("mpv で映像を起動できませんでした。")

    print("再生を開始します (ガイドは映像に追従して自動同期されます)")
    print("  調整: Shift+←/→ ±5秒 / Alt+←/→ ±0.5秒 / Ctrl+←/→ ±0.1秒 / Ctrl+0 でリセット")
    movie_client.set_property("pause", False)
    guide_client.set_property("pause", False)

    last_offset = 0.0
    try:
        while True:
            movie_time = movie_client.get_property("time-pos")
            guide_time = guide_client.get_property("time-pos")
            paused = movie_client.get_property("pause")
            if paused is None or movie_time is None:
                # 映像が終わった (または閉じた) のでガイドも止める
                break
            if guide_time is None:
                # ガイドが最後まで再生された
                break

            if paused:
                guide_client.set_property("pause", True)
            else:
                guide_client.set_property("pause", False)

            # ホットキー (guide_nudge.lua) が書き出した補正値的变化を反映する
            try:
                offset = float(offset_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                offset = last_offset
            if abs(offset - last_offset) > 1e-6:
                last_offset = offset
                guide_start = args.guide_start + last_offset
                guide_client.command(["seek", float(movie_time) + guide_start, "absolute+exact"])
                print(f"  補正 {last_offset:+.2f}s を適用 (ガイド位置 {guide_start:.2f}s)", flush=True)
                continue

            target = float(movie_time) + guide_start
            drift = float(guide_time) - target
            if abs(drift) > args.max_drift:
                guide_client.command(["seek", target, "absolute+exact"])
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n停止します。")
    finally:
        if args.movie and last_offset:
            _save_offset(offsets_path, _movie_key(args.movie), last_offset)
            print(f"補正値 {last_offset:+.2f}s を {offsets_path} に保存しました。")
        guide_client.close()
        movie_client.close()
    return 0


# ---------------------------------------------------------------- ブラウザ位置

class PositionReceiver:
    """ブラウザ拡張 (Firefox) からの再生位置を受け取る HTTP サーバ。

    拡張 (sync/firefox_position) が <video> の currentTime を 0.5 秒ごとに
    POST してきます。指紋照合が不要なので、DB を作る必要がありません。
    """

    def __init__(self, port: int = 8765):
        self.port = port
        self._state: Optional[dict] = None
        self._lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(body.decode("utf-8"))
                except ValueError:
                    payload = {}
                receiver.update(payload)
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    def start(self) -> None:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        try:
            self.server.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def update(self, payload: dict) -> None:
        with self._lock:
            self._state = payload

    def latest(self) -> Optional[dict]:
        with self._lock:
            return self._state


def cmd_position(args) -> int:
    """ブラウザの再生位置に合わせてガイドを再生します (指紋 DB 不要)。"""
    receiver = PositionReceiver(args.port)
    receiver.start()
    print(f"ブラウザの位置を待っています (http://127.0.0.1:{args.port}/position)")
    print("  Firefox 拡張 sync/firefox_position を読み込んでください")
    print("  (about:debugging → This Firefox → Load Temporary Add-on… → manifest.json)")

    guide_client = _start_mpv("guide", args.guide, [
        "--pause=yes", "--audio-display=no", "--no-video",
        f"--start={args.guide_start:.3f}", "--keep-open=no",
    ])
    if guide_client is None:
        sys.exit("mpv を起動できませんでした。")
    print("  再生を開始したら spontaneously ガイドが追従します\n")

    last = None
    try:
        while True:
            state = receiver.latest()
            if state is None:
                time.sleep(0.2)
                continue
            position = float(state.get("currentTime") or 0)
            paused = bool(state.get("paused"))
            target = position + args.guide_start
            guide_time = guide_client.get_property("time-pos")
            if paused:
                guide_client.set_property("pause", True)
            else:
                guide_client.set_property("pause", False)
                if guide_time is None or abs(float(guide_time) - target) > 1.0:
                    guide_client.command(["seek", target, "absolute+exact"])
            if last is None or abs(last - target) > 0.2:
                last = target
                title = (state.get("title") or "")[:40]
                print(f"  位置 {target:8.1f}s  ガイド {title}", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n停止します。")
    finally:
        guide_client.close()
        receiver.stop()
    return 0


# ---------------------------------------------------------------- mux / export

def cmd_mux(args) -> int:
    guide = resolve_guide(args)
    title = args.title or Path(guide).parent.parent.parent.name
    out = args.output or f"{title}_with_guide.mkv"
    with tempfile.TemporaryDirectory(prefix="hmv_sync_") as tmp:
        trimmed = _prepare_guide(guide, args.guide_start, str(Path(tmp) / "guide.mka"))
        ffmpeg = find_ffmpeg()

        # 映画の音声トラック構成を調べる。BD の LPCM (pcm_bluray) は MKV にコピー
        # できないため FLAC に変換し、TrueHD / AC3 / EAC3 などはそのままコピーする。
        movie_streams = json.loads(_run([
            find_ffprobe(), "-v", "error",
            "-show_entries", "stream=index,codec_type,codec_name", "-of", "json", args.movie,
        ]).stdout).get("streams", [])
        movie_audio = [s for s in movie_streams if s.get("codec_type") == "audio"]

        cmd = [ffmpeg, "-v", "error", "-stats",
               "-i", args.movie, "-i", trimmed,
               "-map", "0", "-map", "1:a",
               "-map_metadata", "0",
               "-c:v", "copy", "-c:s", "copy", "-c:d", "copy"]
        for out_index, s in enumerate(movie_audio):
            codec = s.get("codec_name", "")
            if codec in ("pcm_bluray", "pcm_dvd", "pcm_s16le", "pcm_s24le"):
                cmd += ["-c:a:%d" % out_index, "flac", "-compression_level:a:%d" % out_index, "0"]
            else:
                cmd += ["-c:a:%d" % out_index, "copy"]
        guide_index = len(movie_audio)  # 出力におけるガイドの音声インデックス
        cmd += ["-c:a:%d" % guide_index, "copy",
                "-metadata:s:a:%d" % guide_index, "title=HELLO! MOVIE guide (start=%gs)" % args.guide_start,
                "-y", out]
        print("合成を開始します... (映像と TrueHD/AC3 などはコピー、LPCM のみ FLAC に変換します)")
        print("※ Blu-ray (40GB 超) を埋め込むと出力も同サイズになります。")
        proc = subprocess.run(cmd, check=True)
    print(f"完了: {out}")
    print(f"  ガイドは追加の音声トラックです (start = {args.guide_start:g}s)。")
    print("  再生アプリで字幕/音声トラックの切り替えからガイドを選んでください。")
    return proc.returncode


def cmd_export(args) -> int:
    guide = resolve_guide(args)
    out = args.output or (Path(guide).with_name("guide_synced.ogg"))
    _prepare_guide(guide, args.guide_start, str(out))
    print(f"完了: {out}")
    print(f"  映像の 0:00 と同時に再生を始めれば、ガイドは --guide-start {args.guide_start:g}s から始まります。")
    return 0


# ---------------------------------------------------------------- main

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="HELLO! MOVIE 音声ガイドの同期ツール", formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="2つの映像ファイルのオフセットを音声で測定する")
    s.add_argument("--reference", required=True, help="基準となる映像 (例: Netflix の MKV)")
    s.add_argument("--target", required=True, help="比べる映像 (例: Blu-ray の m2ts)")
    s.add_argument("--start", type=float, default=0.0, help="測定開始位置 (秒)")
    s.add_argument("--duration", type=float, default=900.0, help="測定する長さ (秒)")
    s.add_argument("--max-lag", type=float, default=300.0, help="オフセットの探索範囲 ±(秒)")
    s.add_argument("--ref-stream", type=int, default=None)
    s.add_argument("--tgt-stream", type=int, default=None)
    s.set_defaults(func=cmd_scan)

    pl = sub.add_parser("play", help="映像とガイドを同時に再生して同期する (推奨)")
    pl.add_argument("--movie", default=None, help="映像ファイル (BD m2ts / MKV など)")
    pl.add_argument("--guide", default=None, help="ガイドの sound.ogg")
    pl.add_argument("--title-id", type=int, default=None, help="hellomovie_downloads/ からガイドを探す")
    pl.add_argument("--guide-start", type=float, default=0.0,
                    help="映像の 0:00 にガイドの何秒目が来るか (BD: 0 / Netflix: 10)")
    pl.add_argument("--countdown", type=float, default=0.0, help="開始前にカウントダウンする秒数")
    pl.add_argument("--auto", action="store_true", help="保存済みの補正値を自動的に適用する")
    pl.add_argument("--offsets-file", default=None, help="補正値の保存先 (既定: hellomovie_downloads/sync_offsets.json)")
    pl.add_argument("--max-drift", type=float, default=0.25, help="これ以上ずれたらガイドを追い corrective")
    pl.add_argument("--interval", type=float, default=0.25, help="位置を確認・補正する間隔 (秒)")
    pl.add_argument("--no-video-output", action="store_true", help="映像を出力しない (動作確認用)")
    pl.set_defaults(func=cmd_play)

    po = sub.add_parser("position", help="ブラウザの再生位置に合わせてガイドを再生する (指紋 DB 不要)")
    po.add_argument("--guide", required=True, help="ガイドの sound.ogg")
    po.add_argument("--guide-start", type=float, default=0.0,
                    help="映像の 0:00 にガイドの何秒目が来るか (BD: 0 / Netflix: 10)")
    po.add_argument("--port", type=int, default=8765, help="位置を受け取るポート")
    po.add_argument("--interval", type=float, default=0.25, help="位置を確認・追従する間隔 (秒)")
    po.set_defaults(func=cmd_position)

    m = sub.add_parser("mux", help="映像にガイドを合成して1ファイルにする (容量を食うので小容量向け)")
    m.add_argument("--movie", required=True, help="映像ファイル (BD m2ts / MKV など)")
    m.add_argument("--guide", default=None, help="ガイドの sound.ogg")
    m.add_argument("--title-id", type=int, default=None, help="hellomovie_downloads/ からガイドを探す")
    m.add_argument("--guide-start", type=float, default=0.0,
                   help="映像の 0:00 にガイドの何秒目が来るか (BD: 0 / Netflix: 10)")
    m.add_argument("--title", default=None, help="出力ファイル名の元になる作品名")
    m.add_argument("--output", default=None)
    m.set_defaults(func=cmd_mux)

    e = sub.add_parser("export", help="ガイドの先頭を調整したファイルを作る (ブラウザ再生用)")
    e.add_argument("--guide", default=None)
    e.add_argument("--title-id", type=int, default=None)
    e.add_argument("--guide-start", type=float, default=10.0,
                   help="映像の 0:00 にガイドの何秒目が来るか (Netflix なら 10)")
    e.add_argument("--output", default=None)
    e.set_defaults(func=cmd_export)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

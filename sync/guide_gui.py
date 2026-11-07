#!/usr/bin/env python3
"""
guide_gui.py - HELLO! MOVIE 音声ガイドの同期再生 GUI

公式アプリの namesake な操作感を再現したミニアプリです。

  1. 作品を選ぶ (ローカルの token バックアップから取得 / カタログを更新)
  2. 待機音声 (waiting.ogg) を再生する  ← 上映開始前
  3. 映像が始まったら、音声ガイドを自動で同期再生する
     ・手元に映像ファイルがある  : 位置ベース (再生位置をそのまま使う)
     ・外部で再生 (Netflix など)  : 音声認識ベース (指紋照合で位置を推定)

Tkinter だけあれば動きます (標準ライブラリ)。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audio_sync  # noqa: E402
from hellomovie_sync import MpvClient, _start_mpv, find_mpv  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS = ROOT / "hellomovie_downloads"
LUA_SCRIPT = Path(__file__).resolve().parent / "guide_nudge.lua"


# ---------------------------------------------------------------- 作品情報

class TitleLibrary:
    """token バックアップ (tokens.json) から作品一覧を作ります。"""

    def __init__(self, folder: Path = DOWNLOADS):
        self.folder = folder
        self.titles: dict[int, dict[str, Any]] = {}

    def load(self) -> None:
        path = self.folder / "tokens.json"
        if not path.exists():
            self.titles = {}
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.titles = {}
            return
        self.titles = {int(k): v for k, v in (data.get("titles") or {}).items() if str(k).isdigit()}

    def update_catalog(self) -> int:
        """カタログを取り直して token バックアップを更新します (要ネットワーク)。"""
        from hellomovie_downloader import HelloMovieDownloader

        downloader = HelloMovieDownloader("us-east-1", "us-east-1:dbd8d29b-d3e3-48e9-9a9e-a8474060cab",
                                          output_dir=str(self.folder))
        data = downloader.fetch_movies_list()
        self.load()
        return len(data.get("movies", [])) if data else 0

    def guide_dir(self, title_id: int) -> Optional[Path]:
        base = self.folder / "decrypted"
        if not base.exists():
            return None
        for candidate in sorted(base.glob(f"{title_id}_voiceguide_v*")):
            if (candidate / "voiceguide" / "data" / "sound.ogg").exists():
                return candidate / "voiceguide" / "data"
        return None


# ---------------------------------------------------------------- 再生ワーカー

class SyncWorker(threading.Thread):
    """映像とガイドを mpv で再生し、位置を追いかけます。"""

    def __init__(self, movie: str, guide: str, guide_start: float, offset_file: Path,
                 max_drift: float = 0.25, interval: float = 0.25):
        super().__init__(daemon=True)
        self.movie = movie
        self.guide = guide
        self.guide_start = guide_start
        self.offset_file = offset_file
        self.max_drift = max_drift
        self.interval = interval
        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.movie_client: Optional[MpvClient] = None
        self.guide_client: Optional[MpvClient] = None
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        extra = ["--pause=yes"]
        if LUA_SCRIPT.exists():
            extra.append(f"--script={LUA_SCRIPT}")
            extra.append(f"--script-opts=guide_nudge-offset_file={self.offset_file}")
        self.movie_client = _start_mpv("gui-movie", self.movie, extra)
        self.guide_client = _start_mpv("gui-guide", self.guide, [
            "--pause=yes", "--audio-display=no", "--no-video",
            f"--start={self.guide_start:.3f}", "--keep-open=no",
        ])
        if self.movie_client is None or self.guide_client is None:
            self.events.put(("error", "mpv を起動できませんでした (mpv を PATH に追加してください)"))
            self._cleanup()
            return

        self.events.put(("started", None))
        last_offset = 0.0
        guide_start = self.guide_start
        try:
            while not self._stop.is_set():
                movie_time = self.movie_client.get_property("time-pos")
                guide_time = self.guide_client.get_property("time-pos")
                paused = self.movie_client.get_property("pause")
                if paused is None or movie_time is None or guide_time is None:
                    break
                if paused:
                    self.guide_client.set_property("pause", True)
                else:
                    self.guide_client.set_property("pause", False)

                try:
                    offset = float(self.offset_file.read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    offset = last_offset
                if abs(offset - last_offset) > 1e-6:
                    last_offset = offset
                    guide_start = self.guide_start + last_offset
                    self.guide_client.command(["seek", float(movie_time) + guide_start, "absolute+exact"])

                target = float(movie_time) + guide_start
                drift = float(guide_time) - target
                if abs(drift) > self.max_drift:
                    self.guide_client.command(["seek", target, "absolute+exact"])
                    drift = 0.0
                self.events.put(("position", (float(movie_time), float(guide_time), drift)))
                time.sleep(self.interval)
        finally:
            self._cleanup()
            self.events.put(("finished", None))

    def _cleanup(self) -> None:
        for client in (self.guide_client, self.movie_client):
            if client is not None:
                client.close()
        self.guide_client = None
        self.movie_client = None


class FingerprintWorker(threading.Thread):
    """外部再生の音声を認識して、ガイド位置を更新します。"""

    def __init__(self, db: Path, guide: str, guide_start: float, source_kind: str,
                 source_file: Optional[str], window: float, interval: float):
        super().__init__(daemon=True)
        self.db = db
        self.guide = guide
        self.guide_start = guide_start
        self.source_kind = source_kind
        self.source_file = source_file
        self.window = window
        self.interval = interval
        self.events: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._stop = threading.Event()
        self.player: Optional[subprocess.Popen] = None

    def stop(self) -> None:
        self._stop.set()
        if self.player is not None:
            self.player.terminate()

    def run(self) -> None:
        try:
            payload = pickle_load(self.db)
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", f"指紋 DB を読めませんでした: {exc}"))
            return
        db = payload["packed"]
        base = payload.get("start", 0.0)
        source = audio_sync.AudioSource(self.source_kind, self.source_file, self.window)
        last_target = None
        try:
            while not self._stop.is_set():
                samples = source.read()
                if len(samples) < audio_sync.FRAME * 2:
                    time.sleep(0.3)
                    continue
                live = audio_sync.pack_nibbles(audio_sync.quantize(audio_sync.band_features(samples)))
                position, votes, hits = audio_sync.match_position(db, live)
                if position is None or votes < audio_sync.MIN_VOTES or hits < audio_sync.MIN_HITS:
                    self.events.put(("searching", (votes, hits)))
                    time.sleep(0.5)
                    continue
                position += base
                target = position + self.guide_start
                if last_target is None or abs(target - last_target) > 1.0:
                    last_target = target
                    if self.player is None:
                        self.player = audio_sync.play_guide_from(self.guide, target)
                    self.events.put(("recognized", (position, target)))
                time.sleep(self.interval)
        except Exception as exc:  # noqa: BLE001
            self.events.put(("error", str(exc)))
        finally:
            if self.player is not None:
                self.player.terminate()
            self.events.put(("finished", None))


def pickle_load(path: Path):
    import pickle

    return pickle.loads(Path(path).read_bytes())


# ---------------------------------------------------------------- GUI

class GuideApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("HELLO! MOVIE ガイド同期")
        self.root.geometry("960x620")

        self.library = TitleLibrary()
        self.library.load()
        self.worker: Optional[SyncWorker] = None
        self.fingerprint_worker: Optional[FingerprintWorker] = None
        self.offset_file = Path(os.environ.get("TEMP", "/tmp")) / f"hmv-gui-offset-{os.getpid()}.txt"
        self.offset_file.write_text("0.000", encoding="utf-8")
        self.offset = 0.0

        self._build()
        self._refresh_list()
        self.root.after(200, self._poll_events)

    # -------------------------------------------------- UI

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        left = ttk.Frame(outer)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(outer, padding=(10, 0, 0, 0))
        right.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="作品").pack(anchor="w")
        search = ttk.Entry(left)
        search.pack(fill="x", pady=(4, 4))
        search.bind("<KeyRelease>", lambda _e: self._refresh_list(search.get()))
        self.search_var = search

        columns = ("id", "name", "kind")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("id", text="ID")
        self.tree.heading("name", text="作品名")
        self.tree.heading("kind", text="種別")
        self.tree.column("id", width=60, stretch=False)
        self.tree.column("name", width=340)
        self.tree.column("kind", width=110, stretch=False)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_select())

        buttons = ttk.Frame(left)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="カタログを更新", command=self._update_catalog).pack(side="left")
        ttk.Button(buttons, text="復号する", command=self._decrypt_selected).pack(side="left", padx=6)

        ttk.Separator(right).pack(fill="x", pady=(0, 8))
        self.info = ttk.Label(right, text="作品を選んでください", justify="left")
        self.info.pack(fill="x", anchor="w")

        ttk.Label(right, text="映像ファイル (手元にあれば)").pack(anchor="w", pady=(10, 0))
        row = ttk.Frame(right)
        row.pack(fill="x")
        self.movie_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.movie_var).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="選択", command=self._pick_movie).pack(side="left", padx=(6, 0))

        ttk.Label(right, text="ガイドの基準位置 (映像の 0:00 にガイドの何秒目か)").pack(anchor="w", pady=(10, 0))
        row2 = ttk.Frame(right)
        row2.pack(fill="x")
        self.guide_start_var = tk.StringVar(value="0")
        ttk.Spinbox(row2, from_=-60, to=60, increment=0.5, textvariable=self.guide_start_var,
                     width=8).pack(side="left")
        ttk.Label(row2, text="  BD は 0 / Netflix は 10 が目安", foreground="#666").pack(side="left")

        ttk.Label(right, text="再生").pack(anchor="w", pady=(12, 0))
        actions = ttk.Frame(right)
        actions.pack(fill="x")
        ttk.Button(actions, text="待機音声を再生", command=self._play_waiting).pack(side="left")
        ttk.Button(actions, text="映像＋ガイドを再生", command=self._play_sync).pack(side="left", padx=6)
        ttk.Button(actions, text="外部再生に同期 (音声認識)", command=self._play_fingerprint).pack(side="left")
        ttk.Button(actions, text="停止", command=self._stop_all).pack(side="left", padx=6)

        ttk.Label(right, text="調整").pack(anchor="w", pady=(12, 0))
        nudge = ttk.Frame(right)
        nudge.pack(fill="x")
        for label, delta in (("◀ 5秒", -5), ("◀ 0.5秒", -0.5), ("◀ 0.1秒", -0.1),
                             ("リセット", 0), ("0.1秒 ▶", 0.1), ("0.5秒 ▶", 0.5), ("5秒 ▶", 5)):
            ttk.Button(nudge, text=label, command=lambda d=delta: self._nudge(d)).pack(side="left", padx=2)

        ttk.Separator(right).pack(fill="x", pady=12)
        self.status = ttk.Label(right, text="待機中", justify="left")
        self.status.pack(fill="both", expand=True, anchor="nw")

    # -------------------------------------------------- 作品一覧

    def _refresh_list(self, keyword: str = "") -> None:
        self.tree.delete(*self.tree.get_children())
        keyword = keyword.strip()
        for title_id, entry in sorted(self.library.titles.items()):
            name = entry.get("name") or f"(ID {title_id})"
            if keyword and keyword not in name and keyword != str(title_id):
                continue
            kind = "字幕" if entry.get("contenttype") == 1 else "音声ガイド"
            if entry.get("commentary_flag"):
                kind += " / コメ"
            self.tree.insert("", "end", iid=str(title_id), values=(title_id, name, kind))

    def _update_catalog(self) -> None:
        self.status.config(text="カタログを取得しています...")
        self.root.update_idletasks()
        try:
            count = self.library.update_catalog()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("エラー", f"カタログの取得に失敗しました: {exc}")
            return
        self._refresh_list(self.search_var.get())
        self.status.config(text=f"カタログを更新しました ({count} 件)")

    def _selected(self) -> Optional[int]:
        selection = self.tree.selection()
        if not selection:
            return None
        return int(selection[0])

    def _on_select(self) -> None:
        title_id = self._selected()
        if title_id is None:
            return
        entry = self.library.titles.get(title_id, {})
        guide_dir = self.library.guide_dir(title_id)
        lines = [
            f"ID {title_id}  {entry.get('name') or ''}",
            f"言語: {entry.get('lang_name') or '-'}   種別: {'字幕' if entry.get('contenttype') == 1 else '音声ガイド'}",
            f"ガイド: {guide_dir / 'sound.ogg' if guide_dir else '(未復号)'}",
        ]
        if guide_dir is None:
            lines.append("→ 「復号する」で guide を展開します")
        self.info.config(text="\n".join(lines))

    def _decrypt_selected(self) -> None:
        title_id = self._selected()
        if title_id is None:
            messagebox.showinfo("注意", "作品を選んでください")
            return
        import hellomovie_decrypt as hd

        packages = sorted((self.library.folder).glob(f"{title_id}_voiceguide_v*.zip"))
        if not packages:
            messagebox.showinfo("注意", "この作品の ZIP がありません (ダウンロードが必要です)")
            return
        package = packages[0]
        token = self.library.titles.get(title_id, {}).get("token")
        if not token:
            messagebox.showinfo("注意", "この作品の token がありません")
            return
        if hd.decrypt_zip(str(package), hd.default_output_dir(str(package)), int(token)):
            self._on_select()
            self.status.config(text=f"復号しました: {package.name}")
        else:
            messagebox.showerror("エラー", "復号に失敗しました")

    # -------------------------------------------------- 再生

    def _pick_movie(self) -> None:
        path = filedialog.askopenfilename(
            title="映像ファイルを選ぶ",
            filetypes=[("動画", "*.mkv *.m2ts *.mp4 *.avi *.mov"), ("すべて", "*.*")],
        )
        if path:
            self.movie_var.set(path)

    def _guide_paths(self):
        title_id = self._selected()
        if title_id is None:
            messagebox.showinfo("注意", "作品を選んでください")
            return None
        guide_dir = self.library.guide_dir(title_id)
        if guide_dir is None:
            messagebox.showinfo("注意", "ガイドがまだ復号されていません (「復号する」を先に)")
            return None
        return guide_dir / "sound.ogg", guide_dir / "waiting.ogg"

    def _play_waiting(self) -> None:
        paths = self._guide_paths()
        if paths is None:
            return
        _, waiting = paths
        if not waiting.exists():
            messagebox.showinfo("注意", "この作品には待機音声がありません")
            return
        mpv = find_mpv()
        if mpv is None:
            messagebox.showerror("エラー", "mpv が見つかりません")
            return
        subprocess.Popen([mpv, "--no-config", "--no-terminal", str(waiting)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.status.config(text="待機音声を再生しています (上映開始まで)")

    def _play_sync(self) -> None:
        paths = self._guide_paths()
        if paths is None:
            return
        guide, _ = paths
        movie = self.movie_var.get().strip()
        if not movie:
            messagebox.showinfo("注意", "映像ファイルを選んでください")
            return
        self._stop_all()
        self.offset = 0.0
        self.offset_file.write_text("0.000", encoding="utf-8")
        guide_start = float(self.guide_start_var.get() or 0)
        self.worker = SyncWorker(movie, str(guide), guide_start, self.offset_file)
        self.worker.start()
        self.status.config(text="再生を開始します...")

    def _play_fingerprint(self) -> None:
        paths = self._guide_paths()
        if paths is None:
            return
        guide, _ = paths
        movie = self.movie_var.get().strip()
        if not movie:
            messagebox.showinfo("注意", "指紋 DB を作るために映像ファイルが必要です")
            return
        self._stop_all()
        db = self.library.folder / f"fp_{Path(movie).stem}.pkl"
        if not db.exists():
            self.status.config(text="指紋 DB を作成しています (数分かかります)...")
            self.root.update_idletasks()
            try:
                audio_sync.build_db(movie, str(db))
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("エラー", f"指紋 DB の作成に失敗しました: {exc}")
                return
        guide_start = float(self.guide_start_var.get() or 0)
        self.fingerprint_worker = FingerprintWorker(db, str(guide), guide_start, "mic", None, 6.0, 2.0)
        self.fingerprint_worker.start()
        self.status.config(text="音を認識しています (マイク入力)...")

    def _stop_all(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        if self.fingerprint_worker is not None:
            self.fingerprint_worker.stop()
            self.fingerprint_worker = None
        self.status.config(text="停止しました")

    def _nudge(self, delta: float) -> None:
        if delta == 0:
            self.offset = 0.0
        else:
            self.offset += delta
        self.offset_file.write_text(f"{self.offset:.3f}", encoding="utf-8")
        self.status.config(text=f"補正: {self.offset:+.2f} 秒")

    # -------------------------------------------------- イベント

    def _poll_events(self) -> None:
        for worker in (self.worker, self.fingerprint_worker):
            if worker is None:
                continue
            while True:
                try:
                    kind, payload = worker.events.get_nowait()
                except queue.Empty:
                    break
                if kind == "position":
                    movie_time, guide_time, drift = payload
                    self.status.config(text=(
                        f"映像 {movie_time:8.1f}s / ガイド {guide_time:8.1f}s / ずれ {drift:+.3f}s\n"
                        f"補正 {self.offset:+.2f} 秒"
                    ))
                elif kind == "recognized":
                    position, target = payload
                    self.status.config(text=f"認識: 映画内 {position:.1f} 秒 → ガイド {target:.1f} 秒")
                elif kind == "searching":
                    votes, hits = payload
                    self.status.config(text=f"認識中... (得票 {votes} / ヒット {hits})")
                elif kind == "error":
                    self.status.config(text=f"エラー: {payload}")
                elif kind == "finished":
                    self.status.config(text="再生を終了しました")
        self.root.after(200, self._poll_events)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    root = tk.Tk()
    app = GuideApp(root)
    try:
        app.run()
    finally:
        app._stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())

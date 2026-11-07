# 音声ガイドと映像の同期

HELLO! MOVIE の音声ガイド (コメンタリー) を、Blu-ray や Netflix の映像と同期させるためのツール群です。
`sync/hellomovie_sync.py` と `sync/audio_sync.py`、`sync/guide_gui.py` があります。

---

## 公式アプリはどうやって同期しているか

Android アプリ (v3.3.0) の `libsapkit.so` と jadx 展開結果を調べた結果です。

| 段階 | 実装 |
|---|---|
| 入力 | マイク (劇場の環境) |
| 特徴量 | native の `evxfp` 名前空間。`EVXFPFrameCreator::calcFPFrame` / `calcIndexFP` が指紋フレームとインデックス指紋を作る |
| 照合 | `EVXFPRecognizerMain::calcMatchRate` / `calcMatchRateMaxOfSubframes`。DB は `db/packed.db` と `db/packed2.db` |
| DB の復号 | `evxfp::apiKeyManager::DecryptBinary` (AES128)。`updateFPRecognizer` に 2 本の base64 鍵を渡している |
| 判定 | 一致率が `type2_threshold` (作品ごとに 57〜65) 以上、`type2_patterns` (3) 個一致で確定。タイムアウトは `recog_timeout` (1800 秒) |
| 位置合わせ | 確定後は `updateTimeParameter(...)` / `changedPlayTime()` でガイドの再生位置を追い続ける |

つまり公式は「**今流れている音を指紋照合して、作品のどの位置にいるか**」を推定し、
ガイドの再生位置をそれに合わせています。

ただし `packed.db` は AES128 で暗号化、素直な実装では読みません。
そのため本リポジトリでは **手元の映画ファイルから指紋 DB を自作** して同じことをします。

---

## 3 つの方式

### 1. 位置ベース同時再生 (いちばん安定) — `hellomovie_sync.py play`

手元に映像ファイルがあるなら、位置はファイルから分かるので指紋照合は不要です。

```bash
# Blu-ray の m2ts とガイドを同時に再生
python sync/hellomovie_sync.py play --movie "E:\ビデオ\...\00001.m2ts" --guide sound.ogg

# Netflix 用的 (ガイドの先頭 10 秒を切ってある版を使う)
python sync/hellomovie_sync.py play --guide nf_guide.ogg --guide-start 10
```

* `mpv` の IPC で映像の再生位置を 0.25 秒ごとに取得し、ガイドの位置を追い込みます (ドリフト自動補正)
* 一時停止 / シークはガイドにも伝えます。映像が終わればガイドも止まります
* ずれたときは映像側でホットキー: `Shift+←/→` ±5 秒 / `Alt+←/→` ±0.5 秒 / `Ctrl+←/→` ±0.1 秒 / `Ctrl+0` でリセット
* 補正値は `hellomovie_downloads/sync_offsets.json` に保存され、次回 `--auto` で自動適用されます
* `mpv` が無い場合は `ffplay` で簡易再生 (位置補正は不可) にフォールバックします

実測: 60 秒のクリップで、ガイドと映像のずれは **30〜60 ミリ秒** に収まりました
(音声出力のレイテンシ分だけガイドが遅れるので、補正の閾値は 0.25 秒)。

#### メモ: mpv の IPC は Windows では名前付きパイプ

mpv の `--input-ipc-server` は **Windows では TCP ではなく名前付きパイプ**
(`\\.\pipe\<name>`) を使います。Unix ではソケットファイルです。
本ツールは `sync/hellomovie_sync.py` の `MpvClient` が両方に対応しています。

また、この mpv ビルドの Lua には `mp.add_property` が無いので、
`guide_nudge.lua` は補正値を **ファイル** (`--script-opts=guide_nudge-offset_file=...`) に
書き出し、Python 側がそれを読み取る方式にしています。

### 2. 2つの映像のオフセットを自動測定 — `hellomovie_sync.py scan`

Netflix 版と Blu-ray 版の両方を手元に持つ場合、音声の相互相関から
「どちらが何秒分長いか」を自動測定できます。

```bash
python sync/hellomovie_sync.py scan --reference Netflix.mkv --target BD.m2ts
```

48 帯の対数スペクトルを 0.05 秒 hop で並べ、巡回相関のピークを調べます (numpy のみ)。

### 3. 音声認識ベース (公式アプリの思想の移植) — `sync/audio_sync.py`

「今流れている音」を認識して、ガイドを同期再生します。公式と同じく指紋照合方式です。

```bash
# 1) 手元の映画から指紋 DB を作る (区間を並列デコードするので速い)
python sync/audio_sync.py build --movie "E:\ビデオ\...\00001.m2ts" --out fp_movie.pkl

# 2) システム音 / マイクを認識してガイドを同期再生
python sync/audio_sync.py watch --db fp_movie.pkl --guide sound.ogg --input loopback
python sync/audio_sync.py watch --db fp_movie.pkl --guide sound.ogg --input mic
```

* 照合方式: 対数帯 32 本のエネルギーを 4 bit に量子化し、ビット演算でハミング距離を比較 (AudFPrint と同じ考え方)
* 窓は 32 ms hop、6 秒分を一括照合して得票の最頻位置を推定します
* 判定は「最得票が 2 位を分離できているか」で行います (拮抗していれば採用しません)
* ファイル読み込み時の精度: Netflix ローカルの 600 / 120 / 800 / 500 秒の各 6 秒を投入して **いずれも誤差 0.0 秒** で一致
* システム音 (ループバック) 経由: 実際に映画を鳴らして拾い、**300 秒 → 303.0 秒 / 600 秒 → 603.0 秒** で一致
  (キャプチャ開始が再生開始の 3 秒後だったため就是这个値)
* 無音区間 (冒頭直後のロゴ) は得票が閾値未満になり、誤検出しません

#### システム音の取り方

Windows のシステム音は **WASAPI の loopback** で取得します。

* `sounddevice` の WASAPI は loopback に対応していないので `soundcard` を使います
* ブラウザの Netflix であれば、そのままその音を拾えます

```bash
pip install soundcard
```

#### 判定が緩い理由

システム音はサウンドカード側の処理 (ダウンミックス / EQ / 空間効果など) を隔着するので、
ファイルから直接取り込んだ音とはスペクトルが変わります。
そのため一致の閾値 (`MAX_DISTANCE`) を 12 に緩め、得票の集中と 2 位との差で判断しています。

| 入力 | 最小距離の中央値 | 使う閾値 |
|---|---|---|
| ファイル (直接) | 0〜2 | 2 でも十分 |
| システム音 (loopback) | 13〜17 | 12 |

### 補助コマンド

* `export` : ガイドの先頭を何秒か切って、ブラウザで再生しやすいファイルを作ります (Netflix 用)
* `mux` : 映像にガイドを音声トラックとして埋め込みます。**Blu-ray (40GB 超) では出力も同サイズになるので非推奨**

### 0. GUI — `sync/guide_gui.py`

公式アプリと同じ操作順のミニアプリです (Tkinter のみ、標準ライブラリ)。

```bash
python sync/guide_gui.py
```

1. 左のリストから作品を選ぶ (検索可 / 「カタログを更新」で最新の作品一覧を取得)
2. 「待機音声を再生」で上映開始前の `waiting.ogg` を流す
3. 映像が始まったら
   * 「映像＋ガイドを再生」 → 手元の映像ファイルなら位置ベースで自動同期
   * 「自動同期 (音声認識)」 → Netflix など外部再生なら、音を認識して自動でガイドが追従
4. ずれたときは「調整」ボタン (±0.1 / ±0.5 / ±5 秒 / リセット)

作品一覧は `tokens.json` から読むので、**配信終了してカタログから消えた作品も選べます**。

---

## 超かぐや姫！ の実測値

手元にある Netflix ローカル MKV と Blu-ray の m2ts で計測したものです。

| 項目 | 値 |
|---|---|
| Netflix MKV | 8679.104 秒 (Part 1: 0〜8569 / Credits: 8569〜8679) |
| Blu-ray m2ts | 8430.976 秒 (PTS 開始が 4184、音声は LPCM + TrueHD + AC3) |
| 相互相関 | **BD = Netflix + 10.00 秒** (600 / 1200 / 3600 / 5400 / 7200 / 8000 秒で一定、スコア 33〜46/48) |
| フレーム確認 | BD 610 秒 == Netflix 600 秒 (同一フレーム) |
| ガイド (sound.ogg) | 8425.548 秒 / モノラル Opus 48kHz、**冒頭 25 秒と末尾 5 秒が無音** |
| ガイドと BD の長さ差 | 5.4 秒 (BD 側の末尾が長い) |

したがってガイドの `t=0` は本編の `t=0` に相当するとみなせ、

* Blu-ray で再生するなら `--guide-start 0`
* Netflix で再生するなら `--guide-start 10` (Netflix は BD より 10 秒短いぶん)

となります。冒頭 25 秒が無音なので、開始位置が数秒ずれていても、最初に始まるコメンタリーにはほとんど影響しません。

### ガイドの無音リーダーについて

ガイドの 0〜25 秒は無音で、25 秒からコメンタリーが始まります。
(劇場上映前の「待機」用音声は `waiting.dat` として別ファイルに入っています。)

---

## 依存

| 機能 | 必要なもの |
|---|---|
| scan | numpy, ffmpeg |
| play (mpv) | mpv, ffmpeg |
| play (ffplay) | ffmpeg (ffplay) |
| guide_gui | mpv (同期再生する場合), ffmpeg |
| audio_sync build/watch | numpy, ffmpeg、系统音なら soundcard |
| mux / export | ffmpeg (libopus) |

```bash
pip install numpy
# システム音 (ループバック) を使う場合
pip install soundcard
```

mpv は [mpv.io](https://mpv.io/) の Windows ビルド (`winget install shinchiro.mpv`) が
そのまま使えます。PATH に無くても `C:\Program Files\MPV Player\mpv.exe` を
自動で探します。

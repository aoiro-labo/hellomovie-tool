# hellomovie-tool

Unofficial downloader / decryptor for **HELLO! MOVIE** (アプリの字幕・音声ガイド・作品画像).

HELLO! MOVIE アプリが配信している

- **字幕パッケージ** (`contenttype=1`) → PNG 字幕に復号
- **音声ガイド / コメンタリーパッケージ** (`contenttype=2`) → Ogg Opus に復号
- **作品サムネイル画像**

をまとめて取得するための非公式ツールです。FFmpeg などの外部メディアツールは不要で、Python の `requests` だけで動作します。

> [!WARNING]
> 本ツールは非公式・非公認です。配信元・権利者とは一切関係ありません。
> 取得した音声・字幕・画像の権利は各権利者に帰属します。
> **個人利用の範囲を超えた再配布・公開は行わないでください。** 利用は自己責任でお願いします。

---

## hlmv-dl との違い

同種のツールとして [tsukumijima/hlmv-dl](https://github.com/tsukumijima/hlmv-dl) があります。
hlmv-dl の音声復号ロジックは実配信パッケージで検証済みで**正しく動作します**（本リポジトリでも同じアルゴリズムを使用しています）。本ツールはそこを出発点に、**字幕対応と堅牢性**を足した上位互換版です。

| | hlmv-dl | hellomovie-tool |
|---|---|---|
| 音声ガイド / コメンタリー | ✓ | ✓ |
| 待機音声 (`waiting`) | ✓ | ✓ |
| **字幕 (PNG) の復号** | ✗ | ✓ |
| 作品画像 | ✗ | ✓ |
| 復号のみモード | ✗ | ✓ |
| UI | CLI | 対話式 (日本語 / English) |
| 依存パッケージ | httpx2, pydantic, rich, typer | **requests のみ** |
| 認証切れ (401/403) の自動再認証 | ✗ | ✓ |
| カタログの一部が壊れていても継続 | ✗ (1件で全滅) | ✓ |

詳しい検証結果と、hlmv-dl 側に入れられる修正案は [docs/hlmv-dl-review.md](docs/hlmv-dl-review.md) にまとめています。

---

## 動作環境

- Python 3.9 以上
- Windows / macOS / Linux
  - Windows は `run.bat` をダブルクリックするだけで起動できます
  - `requests` が無い場合は起動時に自動インストールを試みます

## セットアップ

```bash
git clone https://github.com/aoiro-labo/hellomovie-tool.git
cd hellomovie-tool
pip install -r requirements.txt
```

Windows:

```bat
run.bat
```

その他:

```bash
python hellomovie_launcher.py
```

## 使い方

```
==================================================
 HELLOMovie ツール
==================================================
  1: ダウンロード
  2: ダウンロード済みファイルの復号
  L: 言語を変更
  Q: 終了
```

### 1. ダウンロード

1. `1` を選択
2. 検索キーワードを入力（空欄で全件）
3. 作品を選択（`1-4` / `1 2 3` / `1, 3, 5` / `all`）
4. 取得するものを選択（`1`: 本体のみ / `2`: 画像のみ / `3`: 両方）
5. 完了後に復号するか確認されるので `y`

保存先は実行ディレクトリの `hellomovie_downloads/` です。
ファイル名には作品名と種類が入ります (`<ID>_<作品名>_<種類>_v<バージョン>`)。

```
hellomovie_downloads/
├── movies.json                     # 作品一覧 (token を含む)
├── tokens.json                     # token バックアップ (消えた作品も保持)
├── 2116_【バリアフリー版】劇場版 魔法少女まどか☆マギカ〈ワルプルギスの廻天〉_音声ガイド_v1.zip
├── 2116_【バリアフリー版】劇場版 魔法少女まどか☆マギカ〈ワルプルギスの廻天〉_画像_v1.png
└── decrypted/
    └── 2116_【バリアフリー版】劇場版 魔法少女まどか☆マギカ〈ワルプルギスの廻天〉_音声ガイド_v1/
        └── voiceguide/
            ├── data/
            │   ├── sound.ogg       # 本編音声
            │   └── waiting.ogg     # 待機音声
            ├── csv/time.csv
            ├── db/packed.db
            └── json/info.json
```

字幕パッケージの場合:

```
hellomovie_downloads/
├── 2115_【バリアフリー版】…_字幕_v1.zip
└── decrypted/
    └── 2115_【バリアフリー版】…_字幕_v1/
        └── subtitles/
            ├── png/1.png, 2.png, ...
            └── png/time.csv
```

> 従来の `<ID>_subtitles_v1.zip` / `<ID>_voiceguide_v1.zip` という名前の ZIP も
> そのまま復号できます (復号済みのフォルダも引き継がれます)。

### 2. ダウンロード済みファイルの復号

ネットワークを使わず、`hellomovie_downloads/` 内の ZIP を選んで復号します。
`movies.json` の `token` を鍵に使うため、先に一度ダウンロードしておく必要があります。

### 言語

初回起動時に日本語 / English を選択できます。以降は `config.json` に保存されます。
コマンドラインからも指定できます。

```bash
python hellomovie_launcher.py --lang en
python hellomovie_launcher.py --mode decrypt
```

---

## token バックアップ (`tokens.json`)

作品ごとの `token` はカタログ (`movies.json`) に載っていますが、**配信が終わると
カタログから作品が消え、token も手元から消えます**。すると保存済みの ZIP が
復号できなくなるため、ダウンロードのたびに `hellomovie_downloads/tokens.json` へ
「一度でも掲載された作品の token と作品名」を蓄積します (消しません)。

```json
{
  "version": 1,
  "updated": "2026-11-07T04:20:31Z",
  "titles": {
    "2144": {
      "token": 1234567890,
      "name": "【コメンタリー版】超かぐや姫！",
      "contenttype": 2,
      "voiceguide_version": 1,
      "subtitle_version": 0,
      "first_seen": "2026-11-01T01:11:00Z",
      "last_seen": "2026-11-01T01:11:00Z"
    }
  }
}
```

復号時は `movies.json` だけでなく `tokens.json` も参照するので、
カタログから消えた作品でも保存済みの ZIP を復号できます。

### token まで失った場合

`tokens.json` にも無い場合は、パッケージの暗号文と平文の構造 (Ogg Opus / PNG のヘッダ)
から token を総当たりで復元できます (CPU 10〜30 分程度)。

```bash
python tools/recover_token.py --zip hellomovie_downloads/2144_voiceguide_v1.zip
```

---

## 音声ガイドと映像の同期

音声ガイド (コメンタリー) を Blu-ray や Netflix の映像に同期させるツール群です。
GUI 付き。公式アプリと同じ操作順です。

```bash
# GUI (作品選択 → 待機音声 → 映像開始で自動同期)
python sync/guide_gui.py

# 映像とガイドを同時に再生 (mpv の IPC でドリフトを自動補正)
python sync/hellomovie_sync.py play --movie "E:\ビデオ\...\00001.m2ts" --guide sound.ogg

# Netflix 版と Blu-ray 版のオフセットを自動測定
python sync/hellomovie_sync.py scan --reference Netflix.mkv --target BD.m2ts

# 「今流れている音」を認識してガイドを同期再生 (公式アプリと同じ考え方)
python sync/audio_sync.py build --movie "E:\ビデオ\...\00001.m2ts" --out fp.pkl
python sync/audio_sync.py watch --db fp.pkl --guide sound.ogg --input loopback
```

mpv があれば位置ベースの同期再生ができます
(`winget install shinchiro.mpv`、PATH に無くても `C:\Program Files\MPV Player\mpv.exe` を自動探索します)。

詳しい仕組みと実測値は [docs/sync.md](docs/sync.md) にまとめています。

---

## 復号アルゴリズム

`libsapkit.so` の `ATCipher` / `EVXPNGCipher` を静的解析して Python に移植したものです。
詳細は [docs/format.md](docs/format.md) を参照してください。

```text
seed  = (((CRC32(name) ^ token) >> 1) & 0xFFFFFFFF) ^ 0xF229
table = XorShift(seed) で生成した 2048 バイト (各要素 1..253)
plain[i] = table[i & 0x7FF] ^ cipher[i]
```

- 音声 (`sound.dat`, `waiting.dat`) はどちらも `name = "sound"` を使います
- 字幕 PNG (`png/<n>.dat`) は `name = "<n>"`（拡張子なし）を使います
- 復号結果は PNG の IHDR CRC / Ogg ページ CRC で検証します

## 既知の制限

- 配信期間が終了した作品はサーバー側で 403 になる場合があります
- 認証は匿名 Cognito Identity を使用します（アカウント連携はしません）
- `config.json` に端末ごとの Cognito Identity ID が保存されます。共有しないでください

## ライセンス

[MIT License](LICENSE)

本ツールは非公式であり、HELLO! MOVIE および関連サービスの商標・著作権は各権利者に帰属します。

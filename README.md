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

```
hellomovie_downloads/
├── movies.json                     # 作品一覧 (token を含む)
├── 2116_voiceguide_v1.zip          # 生パッケージ
├── 2116_image_v1.png               # サムネイル
└── decrypted/
    └── 2116_voiceguide_v1/
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
├── 2115_subtitles_v1.zip
└── decrypted/
    └── 2115_subtitles_v1/
        └── subtitles/
            ├── png/1.png, 2.png, ...
            └── png/time.csv
```

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

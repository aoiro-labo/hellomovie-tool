# hlmv-dl のコードレビューと修正案

対象: [tsukumijima/hlmv-dl](https://github.com/tsukumijima/hlmv-dl) `master` (`160ef7d`, 2026-11-04)
検証日: 2026-11-06

> **2026-11-06 追記**
>
> この文書で提案した 3 件の修正は **PR #1 としてマージされました** (merge commit `fc5c609`)。
> 続けて、保存済みパッケージから音声を復元する `--decode` を **PR #2 で追加**しました
> (merge commit `a659746`、維持者から「LGTM!」)。
>
> つまりカタログ耐性 / Opus 検証 / 403 リトライ / 保存済みパッケージの再復号は
> 上流に反映済みです。**残っている主な差分は字幕対応と token のバックアップ**です。
> 字幕対応は差分が大きいため upstream には提案せず、本リポジトリのみで実装しています。

## 検証方法

1. リポジトリを clone し、`hlmv_dl/__init__.py` の `decode_audio` をそのまま実行
2. 実際に配信されている音声ガイド ZIP 4件（title_id 1998 / 2116 / 2131 / 2144）の
   `sound.dat` と `waiting.dat` を復号し、本ツールの復号結果と SHA-256 で比較
3. signer / catalog エンドポイントの実レスポンスを確認
4. カタログ 319 件の実データでフィルタ条件と必須フィールドを確認

## 結論

**音声の復号ロジックは正しいです。** 誤りは見つかりませんでした。

- 実データ 8 ファイル（sound 4 + waiting 4）すべてで SHA-256 が完全一致
- `token` の符号拡張、seed 計算、XorShift 初期化、鍵テーブル生成、XOR のオフセットすべて一致
- `waiting.dat` が `crc32("sound")` を使う仕様も正しく実装されている
- signer は実際に `Cloudfront-Policy` などのレスポンスヘッダで返すため、現在の取得方法で動作する
  - HTTP ヘッダ名は大文字小文字を区別しないので `CloudFront-*` で取得しても問題ない
- `movies.json` は未認証だと 403 (`MissingKey`) になるため、`--list` で認証するのも正しい

以下は「誤り」ではなく、**将来壊れる可能性がある箇所 / 取りこぼしている機能**です。

---

## 1. 字幕パッケージに非対応（機能の欠落）

カタログ 319 件の内訳は `contenttype=2`（音声）が 169 件、`contenttype=1`（字幕）が **150 件**です。
hlmv-dl は `contenttype == 2` のみを対象にしているため、字幕は 1 件も取得できません。

字幕 ZIP の `subtitles/png/<n>.dat` は音声と同じ ATCipher で、`name` に拡張子なしのファイル名を使います。
本リポジトリの `hellomovie_decrypt.py` に実装があります。

## 2. カタログ 1 件の不正で全件失敗（中）

```python
catalog = Catalog.model_validate_json(response.content)
```

`Movie` は `token`, `lang_name`, `external_bucket` などを必須にしているため、
将来どれか 1 件でも `null` や欠損があると **`--list` もダウンロードも全滅**します。
現在の 319 件はすべて揃っていますが、サーバー側の追加次第で発生します。

修正案: 1件ずつ検証して、壊れているものだけスキップする。

```python
from pydantic import TypeAdapter, ValidationError

adapter = TypeAdapter(Movie)
movies: list[Movie] = []
for item in payload.get('movies', []):
    try:
        movies.append(adapter.validate_python(item))
    except ValidationError:
        continue  # もしくは警告を出す
```

## 3. Ogg の検証が固定オフセット（低〜中）

```python
if decoded[:4] != b'OggS' or decoded[28:36] != b'OpusHead':
```

`decoded[28:36]` は「先頭ページのセグメント数が 1」であることを前提にしています。
現在の配信ファイルはすべて `seg_count == 1` なので動きますが、構造が変わると
**正常なファイルを誤って拒否**します。

修正案: セグメントテーブルを読んで先頭パケットの位置を計算する。

```python
if decoded[:4] != b'OggS':
    raise RuntimeError('Failed to restore Ogg Opus stream.')
segments = decoded[26]
packet = 27 + segments
if decoded[packet:packet + 8] != b'OpusHead':
    raise RuntimeError('Failed to restore Ogg Opus stream.')
```

さらに、先頭ページだけでなく **Ogg ページの CRC** まで見ると破損を検出できます
（実装は `hellomovie_decrypt.py` の `ogg_crc` / `is_valid_ogg`）。

## 4. 署名 Cookie 失効時にリトライしない（中）

`authenticated_until = time.monotonic() + 50 * 60` で 1 時間より前に更新する設計は妥当ですが、
大量バッチ・スリープ復帰・時計ずれなどで 403 になると、そのまま全体が終了します。

修正案: `stream` が 403 を返したら `authenticate()` のキャッシュを切って 1 回だけ再試行する。

```python
self.authenticated_until = 0.0
self.authenticate()
```

`download()` の中で `HTTPStatusError` の `status_code in (401, 403)` を拾って再試行すると安全です。

## 5. ZIP 内パスが固定（低）

```python
archive.read('voiceguide/data/sound.dat')
```

現在のパッケージはすべてこのパスですが、ルートフォルダが付く・階層が変わるなどの
変更で `KeyError` になります。

修正案: 末尾一致で探す。

```python
def find_entry(archive: ZipFile, suffix: str) -> str | None:
    return next((n for n in archive.namelist() if n == suffix or n.endswith('/' + suffix)), None)
```

## 6. 待機音声の存在チェックがダウンロード後（低）

```python
destinations = [audio_path, package_path, metadata_path] if keep_package else [audio_path]
...
if waiting_path.exists() is True:   # ← ZIP を全部落とした後
    raise FileExistsError(...)
```

`waiting.opus` だけが存在する場合、パッケージを最後までダウンロードしてから失敗します。
事前に `waiting_path.exists()` も確認するか、書き込み直前ではなく DL 前に弾くと無駄が減ります。
（待機音声を含まないパッケージでは誤検知になるため、許容するなら現状維持でも可）

## 7. 細かい点

- `Date` ヘッダが無い場合 `KeyError` になり、`Error: 'Date'` という分かりにくい表示になる
- 音声 2 ファイルを順に書くため、2 つ目の書き込みで失敗すると片方だけ残る
  （再実行すると `FileExistsError` になる）
- Windows の予約名 (`CON`, `PRN`, `NUL`, `COM1` など) と 255 バイト超のファイル名は未処理
- `console.is_terminal is False` のとき進捗が完全に消えるため、CI では進捗が見えない

いずれも現状の配信データでは問題になりません。

---

## 本リポジトリでの対応

| 項目 | 対応 | 上流 |
|---|---|---|
| 字幕 | `hellomovie_decrypt.py` で PNG 復号に対応 | 未対応 (差分が大きいため本リポジトリのみ) |
| カタログ欠損 | `movies.json` を `.get()` ベースで処理し、欠損しても継続 | ✅ PR #1 で反映 |
| Ogg 検証 | ページ CRC まで検証 (`is_valid_ogg`) | ✅ PR #1 で反映 |
| 403 | `download_file` / `fetch_movies_list` で再認証して 1 回リトライ | ✅ PR #1 で反映 |
| 保存済みパッケージの再復号 | `tools/recover_token.py` で token を総当たり復旧 | ✅ PR #2 (`--decode`) で反映 |
| token のバックアップ | `tokens.json` に一度でも掲載された	token を蓄積 | 未対応 (hlmv-dl は `--keep-package` の `.info.json` に token を保存) |
| ZIP パス | 正規表現で `*/png/<n>.dat` / `*/data/(sound|waiting).dat` を探す | 該当なし |
| 待機音声 | ZIP を丸ごと保存する設計のため該当なし | 該当なし |
| ファイル名 | `<title_id>_<作品名>_<種別>_v<n>` 形式 (使用禁止文字は `_` に置換) | `<name> [<id>]` 形式 |

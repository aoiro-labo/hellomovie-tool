# HELLO! MOVIE パッケージ形式と復号手順

Android アプリ `HELLO! MOVIE` (v3.3.0) の `libsapkit.so` を静的解析して判明した内容です。
本リポジトリの `hellomovie_decrypt.py` はこの手順をそのまま実装しています。

## 配信パッケージ

| 種別 | カタログ | URL | ZIP 内の暗号化ファイル |
|---|---|---|---|
| 字幕 | `contenttype == 1` | `/{prefix}/{title_id}/{subtitle_version}/subtitles.zip` | `subtitles/png/<n>.dat` |
| 音声ガイド | `contenttype == 2` | `/{prefix}/{title_id}/{voiceguide_version}/voiceguide.zip` | `voiceguide/data/sound.dat`, `voiceguide/data/waiting.dat` |
| 画像 | — | `/{prefix}/{title_id}/{image_version}/{image_filename}` | 平文 PNG |

`prefix` は `external_bucket` によって決まります。

| `external_bucket` | prefix |
|---|---|
| `original` | `v2/movie` |
| `deluxe` / `hellomovie-app-deluxe-demo` | `deluxe` |
| その他 (`hellomovie-app-disney` など) | `movie` |

## 鍵生成 (ATCipher)

作品ごとの `token`（`movies.json` の `token`）と暗号化ファイル名から 2048 バイトの鍵テーブルを作ります。

```text
name  = 音声は常に "sound"、PNG は拡張子なしのファイル名 ("1220" など)
seed  = (((CRC32(name) ^ token) >> 1) & 0xFFFFFFFF) ^ 0xF229
```

- `token` は Java の `int` を `long` に符号拡張した値として扱う（負値は 64bit の2の補数）
- `CRC32` は zlib の CRC-32

### XorShift 初期化

MT 風の線形合同法で 32bit の状態を 4 つ作ります。

```c
uint32_t x = seed;
for (uint32_t k = 1; k <= 4; k++) {
    x = 1812433253 * (x ^ (x >> 30)) + k;
    state[k - 1] = x;
}
```

### 鍵テーブル生成

64bit の xorshift128 で値を進め、`double` に変換して 1〜253 のバイトを作ります。

```c
for (int i = 0; i < 2048; i++) {
    uint64_t t = x0 ^ (x0 << 11);
    uint64_t w = x3 ^ (x3 >> 19) ^ t ^ (t >> 8);
    x0 = x1; x1 = x2; x2 = x3; x3 = w;
    // C++ の double 演算と同じ順序で計算する
    table[i] = (uint8_t)(1.0 + ((double)w * 253.0) * pow(2.0, -64));
}
```

Python では `int(1.0 + (float(w) * 253.0) * 2.0 ** -64)` とします。
float の丸めを変えると 1 バイトずれることがあるため、式の順序を変えないでください。

### 復号

鍵テーブルを先頭から繰り返し XOR するだけです。

```text
plain[i] = table[i % 2048] ^ cipher[i]
```

音声の `waiting.dat` も **`name = "sound"`** を使います（`"waiting"` ではありません）。
ここは間違えやすいポイントです。

## 復号結果の検証

先頭数バイトを見るだけでは不十分なので、本ツールは以下で検証しています。

- **PNG**: シグネチャ + `IHDR` チャンクの CRC-32
  - 先頭 8 バイトは token に関係なく必ず `01` になるため、シグネチャだけでは正否を判定できない
- **Ogg Opus**: 先頭ページの CRC-32（Ogg の CRC は初期値 0、多項式 `0x04C11DB7`）
  - ページの CRC フィールドを 0 にして計算し、保存値と比較する

## カタログと認証

1. `POST https://cognito-identity.us-east-1.amazonaws.com/` で匿名 Identity ID を取得
   - `X-Amz-Target: AWSCognitoIdentityService.GetId`
   - `IdentityPoolId: us-east-1:dbd8d29b-d3e3-48e9-9a9e-a8474060acab`
2. `POST https://app-signer.hellomovie.app/app_signer` に `IdentityId` ヘッダと `{"app_id":"hellomovie"}` を送る
3. レスポンスの `Cloudfront-Policy` / `Cloudfront-Signature` / `Cloudfront-Key-Pair-Id` を
   `app.hellomovie.app` の Cookie として保存
4. `GET https://app.hellomovie.app/v2/movie/movies.json` でカタログ取得

> Cookie が無いと `movies.json` も 403 (`MissingKey`) を返します。

署名 Cookie の有効期限は約1時間です。長時間のバッチでは 401/403 を受けたら再認証してください。

# 公式アプリの指紋 DB (`packed.db`) について

公式 HELLO! MOVIE アプリがmovies 音声を認識する時に使う指紋 DB
(`voiceguide/db/packed.db`, `packed2.db`) を自前で使うための調査結果です。

**現状: 復号には至っていない。** ただし内部構造は相当分かっています。

---

## 1. DB は暗号化されたバイナリ

| | |
|---|---|
| サイズ | `packed.db` 約 1 MB / `packed2.db` 約 2.9 MB (作品ごと) |
| 16 バイト境界 | ちょうど倍数 (AES-128-ECB と整合) |
| バイト分布 | 完全一様 (4バイト単位の小さい値は 0 個) |
| 先頭 16 バイト | **全作品で同一** (`e817d09ef03331d06a447d1b14816a83` / packed2 は `af660830…`) |

→ 平文のテーブル (タイムスタンプ等) ではなく、暗号化されたデータです。

## 2. native 側の構造 (`libsapkit.so`)

 reversing した名前空間は `evxfp` (Evixar FingerPrint):

```
evxfp::apiKeyManager::DecryptBinary(vector<uint8>, vector<uint8>)
evxfp::apiKeyManager::Decrypt(string, string)
evxfp::apiKeyManager::Encrypt(string, string)
evxfp::AES128_ECB_decrypt(uint8*, uint8*, uint32)
evxfp::AES128_ECB_encrypt(uint8*, uint8*, uint32)
evxfp::base64_decode / base64_encode
evxfp::getEVXFPParam(const char*)          @ 0x000dbe15 (1608 bytes)
evxfp::apiKeyManager::apiKeyManager(const char*) @ 0x000e6249 (432 bytes)
evxfp::EVXFPRecognizerMain::initialize(const char*, char*, char*) @ 0x000dddf9
evxfp::EVXFPRecognizerMain::setParamFromPackedDB(EVXFPParam*, uint8 const*) @ 0x000de4af
evxfp::EVXFPRecognizerMain::readTargetData(uint8 const*)
evxfp::EVXFPRecognizerMain::prepareFPFrames / calcMatchRate / calcMatchRateMaxOfSubframes
evxfp::EVXFPFrameCreator::calcFPFrame / calcIndexFP
```

Java 側 (`F3/T.java`) は APК のアセット位置を渡すだけです:

```java
E eC = c(K.h(), "packed.db");                 // APK パス + アセットの offset/length
updateFPRecognizer(
    "dnRaeeIGIRt6hG7ADmrT29rEKX9kvpdQYgsGj0Xyi2arycBm3OfvxY+GJqd5dL/d7Y3zlHPCbh+dyNnb77BMFECcyaAMjQqXHS03nWy46ZXCmpk9ZPU3XeDTFFKT/+wf",  // 96 バイト
    "XXBU6d12wboOvdLX4tVjY8NJBBcs7/Bzx9wC6djwhkN5A6unptD9om8okI5I0Ic7xBmNLj7uRonJDozw4VKcZw=",                                // 64 バイト
    57L, 3L, str, eC.f1450b, eC.f1451c);
```

この 2 本の base64 鍵は Java 側にハードコードがあり、**.so には無い**です。

## 3. 鍵候補

`.rodata` に.base64 風 128 文字 (96 バイト) の定数が 1 つ:

```
0x00074ef5
iJBP8RHZgYJHZC7DW7bTXn3Bb7t4ARgMkFsWK48xL9gwcPmQNryADSNZFcjpCA2j2AHbRYrdKFsUYNZ5F8AZ5amVnx6UdRPnJmA6urHp2g7YgEdL3pFTsC7D7xpf2Dc8
```

`apiKeyManager::apiKeyManager(const char*)` の引数 (マスターキーの候補) と
思われます。

## 4. 試行したこと (すべて不達)

* base64 のスライス / md5 / sha1 / sha256 / XOR 組み合わせ (2段構えも含む) で
  AES-128-ECB / CBC-0 を総当たり → 復号結果は乱数と区別できず
* `libsapkit.so` から抽出した 47 個の定数を鍵候補に likewise → 不達

## 5. 残りの作業 (2 段階)

1. **鍵導出の解析** — `apiKeyManager::Decrypt` / `getEVXFPParam` を読む。
   マスターキー (96 バイト) から 16 バイトの AES 鍵をどう作っているか。
   `tools/re/arm_dis.py` (このリポジトリに同梱) で逆アセンブルできる。
2. **evxfp 特徴量の再現** — 復号しても、DB の指紋が
   `EVXFPFrameCreator::calcFPFrame` / `calcIndexFP` で作られた
   proprietary 形式。自前の帯エネルギー特徴量では照合できません。
   `calcMatchRate` のロジック (どのハッシュを何种で照合するか) を読む必要があります。

つまり「DB を読める」だけでは足りず、**照合アルゴリズムの再現**も要ります。

## 6. 実用上の代替 (実装済み)

| 場面 | 使う方法 | 精度 |
|---|---|---|
| ブラウザ再生 (Netflix 等) | Firefox 拡張で `<video>` の位置を直接読む (`sync/firefox_position/`) | 誤差 0 (位置そのもの) |
| 手元の映像ファイル | mpv の IPC で位置を取る | 誤差 30〜60 ms |
| 劇場 / BD プレーヤー | 音声認識 (自前の指紋 DB) | 誤差 0.3 秒 |

最後の「劇場」だけが公式 DB の存在意義のある場所です。そこには
上の 2 段階の reversing が必要になります。


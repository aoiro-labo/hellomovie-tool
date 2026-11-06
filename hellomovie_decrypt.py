import os
import re
import sys
import json
import struct
import zlib
import zipfile
import logging
import argparse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import hellomovie_i18n
from hellomovie_i18n import tr

# ログの設定
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 定数宣言
# libsapkit.so (ATCipher / EVXPNGCipher) の解析結果に基づく。
#   seed  = (((CRC32(name) ^ token) >> 1) & 0xFFFFFFFF) ^ 0xF229
#   table = XorShift(seed) で生成した 2048 バイト (各バイトは 1〜253)
#   plain[i] = table[i & 0x7FF] ^ cipher[i]
TABLE_SIZE = 2048
SEED_XOR = 0xF229
MASK32 = 0xFFFFFFFF
MASK64 = (1 << 64) - 1
AUDIO_CIPHER_NAME = "sound"  # sound.dat / waiting.dat はどちらもこの名前の CRC32 を使う
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

PNG_ENTRY_RE = re.compile(r"(?:^|/)png/(\d+)\.dat$")
AUDIO_ENTRY_RE = re.compile(r"(?:^|/)data/(sound|waiting)\.dat$")
# 新命名: <ID>_<作品名>_字幕_v1.zip / <ID>_<作品名>_音声ガイド_v1.zip
ZIP_NAME_RE = re.compile(r"^(\d+)_(.+)_(字幕|音声ガイド)_v\d+\.zip$")
# 旧命名 (<ID>_subtitles_v1.zip など) の後方互換用
LEGACY_ZIP_NAME_RE = re.compile(r"^(\d+)_(subtitles|voiceguide)_v\d+\.zip$")
DECRYPTED_DIR_NAME = "decrypted"
# 作品ごとの token を蓄積するバックアップ (カタログから作品が消えても復号できるようにする)
TOKEN_BACKUP_NAME = "tokens.json"


def zip_title_id(filename: str) -> Optional[int]:
    """
    ファイル名から title_id を取り出す (新命名と旧命名の両方に対応)。
    """
    m = ZIP_NAME_RE.match(filename) or LEGACY_ZIP_NAME_RE.match(filename)
    return int(m.group(1)) if m else None


def make_seed(name: str, token: int) -> int:
    """
    暗号化名 (PNG は拡張子なしのファイル名、音声は "sound") と作品の token から seed を作ります。
    Java 側の token は int を long へ符号拡張して渡されるため、64bit の2の補数として扱います。
    """
    crc = zlib.crc32(name.encode("utf-8"))
    return (((crc ^ (token & MASK64)) >> 1) & MASK32) ^ SEED_XOR


def build_key_table(seed: int) -> bytes:
    """
    seed から 2048 バイトの鍵テーブルを生成します (ATCipher::init と同じ手順)。
    XorShift は 64bit 要素の xorshift128 で、初期化は MT 風の線形合同法です。
    """
    x = seed & MASK32
    state: List[int] = []
    for k in range(1, 5):
        x = ((x ^ (x >> 30)) * 0x6C078965 + k) & MASK32
        state.append(x)
    x0, x1, x2, x3 = state

    table = bytearray(TABLE_SIZE)
    for i in range(TABLE_SIZE):
        t = (x0 ^ (x0 << 11)) & MASK64
        w = (x3 ^ (x3 >> 19) ^ t ^ (t >> 8)) & MASK64
        x0, x1, x2, x3 = x1, x2, x3, w
        table[i] = int(1.0 + (float(w) * 253.0) * 2.0 ** -64)
    return bytes(table)


def xor_with_table(data: bytes, table: bytes, start: int = 0) -> bytes:
    """
    table[(start + i) & 0x7FF] を data の各バイトへ XOR します (暗号化・復号で共通)。
    """
    n = len(data)
    if n == 0:
        return b""
    offset = start % TABLE_SIZE
    key = (table[offset:] + table * (n // TABLE_SIZE + 1))[:n]
    return (int.from_bytes(data, "little") ^ int.from_bytes(key, "little")).to_bytes(n, "little")


def decrypt_bytes(data: bytes, name: str, token: int) -> bytes:
    return xor_with_table(data, build_key_table(make_seed(name, token)))


def _build_ogg_crc_table() -> List[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) & MASK32 if r & 0x80000000 else (r << 1) & MASK32
        table.append(r)
    return table


_OGG_CRC_TABLE = _build_ogg_crc_table()


def ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & MASK32) ^ _OGG_CRC_TABLE[((crc >> 24) & 0xFF) ^ b]
    return crc


def is_valid_png(data: bytes) -> bool:
    """
    PNG シグネチャと IHDR チャンクの CRC を確認します。
    先頭8バイトの鍵は seed に関係なく全て 0x01 になるため、シグネチャだけでは token の正否を判定できません。
    """
    if len(data) < 33 or data[:8] != PNG_SIGNATURE:
        return False
    length, ctype = struct.unpack(">I4s", data[8:16])
    if length != 13 or ctype != b"IHDR":
        return False
    return zlib.crc32(data[12:29]) == struct.unpack(">I", data[29:33])[0]


def is_valid_ogg(data: bytes) -> bool:
    """
    先頭の Ogg ページの CRC を確認します。
    """
    if len(data) < 27 or data[:4] != b"OggS":
        return False
    seg_count = data[26]
    header_len = 27 + seg_count
    if len(data) < header_len:
        return False
    page_len = header_len + sum(data[27:header_len])
    if len(data) < page_len:
        return False
    page = bytearray(data[:page_len])
    stored = struct.unpack("<I", page[22:26])[0]
    page[22:26] = b"\x00\x00\x00\x00"
    return ogg_crc(bytes(page)) == stored


def load_tokens(movies_json_path: str) -> Dict[int, int]:
    """
    movies.json から {title_id: token} を作ります。token が無い作品は 0 として扱います (アプリ側の既定値)。

    同じフォルダに tokens.json (token バックアップ) があればそちらも読み込み、
    カタログから消えた作品 (配信終了など) でも復号できるようにします。
    """
    with open(movies_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    tokens = {int(m["title_id"]): int(m.get("token") or 0) for m in data.get("movies", []) if "title_id" in m}

    backup_path = os.path.join(os.path.dirname(os.path.abspath(movies_json_path)), TOKEN_BACKUP_NAME)
    for title_id, token in load_token_backup(backup_path).items():
        tokens.setdefault(title_id, token)
    return tokens


def load_token_backup(backup_path: str) -> Dict[int, int]:
    """
    token バックアップ (tokens.json) から {title_id: token} を読み込みます。
    """
    if not os.path.exists(backup_path):
        return {}
    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    tokens: Dict[int, int] = {}
    for title_id, entry in (data.get("titles") or {}).items():
        try:
            token = int(entry.get("token") or 0)
        except (TypeError, ValueError):
            continue
        if token:
            tokens[int(title_id)] = token
    return tokens


def load_title_backup(backup_path: str) -> Dict[int, Dict[str, Any]]:
    """
    token バックアップから {title_id: 作品情報} を読み込みます (作品名も保持しているため)。
    """
    if not os.path.exists(backup_path):
        return {}
    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    titles: Dict[int, Dict[str, Any]] = {}
    for title_id, entry in (data.get("titles") or {}).items():
        try:
            titles[int(title_id)] = entry
        except (TypeError, ValueError):
            continue
    return titles


def update_token_backup(movies: List[Dict[str, Any]], backup_path: str) -> int:
    """
    カタログの token をバックアップ (tokens.json) に追記します。

    一度でも配信されていた作品的信息は消さないので、配信終了してカタログから
    消えた作品でもZIPが残っていれば後から復号できます。

    Returns:
        int: バックアップに保持されている作品数
    """
    existing: Dict[str, Dict[str, Any]] = {}
    if os.path.exists(backup_path):
        try:
            with open(backup_path, "r", encoding="utf-8") as f:
                existing = dict((json.load(f) or {}).get("titles") or {})
        except (OSError, ValueError):
            existing = {}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = 0
    for movie in movies:
        title_id = movie.get("title_id")
        token = movie.get("token")
        if title_id is None or not token:
            continue
        key = str(title_id)
        entry = existing.get(key)
        if entry is None:
            entry = {"first_seen": now}
            added += 1
        entry.update({
            "token": int(token),
            "name": movie.get("name"),
            "lang_name": movie.get("lang_name"),
            "contenttype": movie.get("contenttype"),
            "voiceguide_version": movie.get("voiceguide_version"),
            "subtitle_version": movie.get("subtitle_version"),
            "last_seen": now,
        })
        existing[key] = entry

    payload = {"version": 1, "updated": now, "titles": existing}
    try:
        tmp_path = backup_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=1, ensure_ascii=False, sort_keys=True)
        os.replace(tmp_path, backup_path)
    except OSError:
        return len(existing)
    return len(existing)


def default_output_dir(zip_path: str) -> str:
    """
    ZIP の復号先 (ZIPのあるフォルダ/decrypted/ZIP名) を返します。
    """
    zip_dir = os.path.dirname(os.path.abspath(zip_path))
    stem = os.path.splitext(os.path.basename(zip_path))[0]
    return os.path.join(zip_dir, DECRYPTED_DIR_NAME, stem)


def _safe_join(base_dir: str, entry_name: str) -> Optional[str]:
    """
    ZIP エントリ名を base_dir 配下の保存先パスへ変換します。配下から出るパスは None を返します。
    """
    base = os.path.abspath(base_dir)
    dest = os.path.abspath(os.path.join(base, *entry_name.split("/")))
    if dest != base and not dest.startswith(base + os.sep):
        return None
    return dest


def decrypt_zip(zip_path: str, out_dir: str, token: int) -> bool:
    """
    字幕ZIP / 音声ガイドZIP を展開し、暗号化された .dat を復号して保存します (png/N.dat -> png/N.png、
    data/sound.dat -> data/sound.ogg)。それ以外のファイルはそのままコピーします。
    各種別の最初の1件で復号結果を検証し、失敗した場合は token が違うものとして中断します。
    """
    os.makedirs(out_dir, exist_ok=True)
    verified = {"png": False, "audio": False}
    png_count = audio_count = bad_count = 0

    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            dest = _safe_join(out_dir, info.filename)
            if dest is None:
                logger.warning(tr(
                    f"不正なパスのエントリを無視しました: {info.filename}",
                    f"Ignored an entry with an unsafe path: {info.filename}"
                ))
                continue
            raw = zf.read(info)

            png_match = PNG_ENTRY_RE.search(info.filename)
            audio_match = AUDIO_ENTRY_RE.search(info.filename)
            if png_match:
                kind, name, ext, check = "png", png_match.group(1), ".png", is_valid_png
            elif audio_match:
                kind, name, ext, check = "audio", AUDIO_CIPHER_NAME, ".ogg", is_valid_ogg
            else:
                kind = None

            if kind:
                data = decrypt_bytes(raw, name, token)
                ok = check(data)
                if not verified[kind]:
                    if not ok:
                        logger.error(tr(
                            f"{info.filename} の復号結果が不正です。token ({token}) が違う可能性があります。",
                            f"The decrypted result of {info.filename} is invalid. The token ({token}) may be wrong."
                        ))
                        return False
                    verified[kind] = True
                if not ok:
                    bad_count += 1
                    logger.warning(tr(
                        f"復号結果の検証に失敗しました: {info.filename}",
                        f"Verification of the decrypted result failed: {info.filename}"
                    ))
                dest = os.path.splitext(dest)[0] + ext
                if kind == "png":
                    png_count += 1
                else:
                    audio_count += 1
            else:
                data = raw

            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)

    logger.info(tr(
        f"完了: {zip_path} -> {out_dir} (PNG {png_count} 件, 音声 {audio_count} 件, 検証失敗 {bad_count} 件)",
        f"Done: {zip_path} -> {out_dir} (PNG: {png_count}, audio: {audio_count}, verification failures: {bad_count})"
    ))
    return bad_count == 0


def main() -> int:
    # --help の文言も同じ言語で出すため、--lang だけ先に読む (指定が無ければ設定ファイルの保存値)
    hellomovie_i18n.init_language(override=hellomovie_i18n.preparse_language())

    parser = argparse.ArgumentParser(description=tr(
        "HELLOMovie の字幕ZIP / 音声ガイドZIP を復号します。",
        "Decrypt HELLOMovie subtitle / audio guide ZIP files."
    ))
    parser.add_argument("zips", nargs="+", help=tr(
        "hellomovie_downloader.py で保存した ZIP (例: 1997_【映画名】_字幕_v1.zip)",
        "ZIP files saved by hellomovie_downloader.py (e.g. 1997_subtitles_v1.zip)"
    ))
    parser.add_argument("-o", "--output", help=tr(
        "出力先フォルダ (既定: ZIPのあるフォルダ/decrypted/ZIP名)",
        "Output folder (default: <ZIP folder>/decrypted/<ZIP name>)"
    ))
    parser.add_argument("--token", type=int, help=tr(
        "作品の token を直接指定 (movies.json より優先)",
        "Specify the title's token directly (takes precedence over movies.json)"
    ))
    parser.add_argument("--movies", help=tr(
        "movies.json のパス (既定: ZIPのあるフォルダ/movies.json)",
        "Path to movies.json (default: <ZIP folder>/movies.json)"
    ))
    parser.add_argument("--lang", choices=sorted(hellomovie_i18n.SUPPORTED_LANGUAGES), help=tr(
        "表示言語 (既定: config.json の保存値、無ければ ja)",
        "Display language (default: the value saved in config.json, otherwise ja)"
    ))
    args = parser.parse_args()

    all_ok = True
    for zip_path in args.zips:
        token = args.token
        if token is None:
            title_id = zip_title_id(os.path.basename(zip_path))
            movies_path = args.movies or os.path.join(os.path.dirname(os.path.abspath(zip_path)), "movies.json")
            if not title_id or not os.path.exists(movies_path):
                logger.error(tr(
                    f"token を決められません: {zip_path} (--token か movies.json を指定してください)",
                    f"Cannot determine the token for {zip_path} (specify --token or provide movies.json)"
                ))
                all_ok = False
                continue
            token = load_tokens(movies_path).get(title_id)
            if token is None:
                logger.error(tr(
                    f"movies.json に title_id {title_id} がありません",
                    f"title_id {title_id} is not in movies.json"
                ))
                all_ok = False
                continue
        out_dir = args.output or default_output_dir(zip_path)
        all_ok = decrypt_zip(zip_path, out_dir, token) and all_ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

import os
import sys
import json
import re
import logging
import requests
from typing import Dict, Any, List, Optional, Set, Tuple

import hellomovie_decrypt
import hellomovie_i18n
from hellomovie_i18n import tr

# ログの設定
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 定数宣言
BASE_URL = "https://app.hellomovie.app"
SIGNER_URL = "https://app-signer.hellomovie.app/app_signer"
DEFAULT_OUTPUT_DIR = "./downloads"
CONFIG_FILE = "config.json"

# AWS 設定
AWS_REGION = "us-east-1"
IDENTITY_POOL_ID = "us-east-1:dbd8d29b-d3e3-48e9-9a9e-a8474060acab"

# 保存先フォルダ
SAVE_DIRECTORY = "./hellomovie_downloads"

class HelloMovieDownloader:
    """
    HELLOMovie配信サーバーからのファイル取得および対話式ダウンロード処理を行うクラス。
    """

    def __init__(self, region: str, identity_pool_id: str, output_dir: str = DEFAULT_OUTPUT_DIR):
        """
        :param region: AWS リージョン (例: "us-east-1")
        :param identity_pool_id: AWS Cognito Identity Pool ID
        :param output_dir: ダウンロードファイルの保存先ディレクトリ
        """
        self.region = region
        self.identity_pool_id = identity_pool_id
        self.cognito_identity_id = self.load_cached_identity_id()
        self.output_dir = output_dir
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "HELLOMovie/2.0",
            "Accept": "*/*"
        })
        os.makedirs(self.output_dir, exist_ok=True)

    def load_cached_identity_id(self) -> Optional[str]:
        """
        設定ファイル (config.json) から保存済みの Cognito Identity ID を読み込みます。
        """
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    identity_id = data.get("identity_id")
                    if identity_id:
                        logger.info(tr(
                            f"設定ファイルから保存済み Cognito Identity ID を読み込みました: {identity_id}",
                            f"Loaded the saved Cognito Identity ID from the config file: {identity_id}"
                        ))
                        return identity_id
            except Exception as e:
                logger.warning(tr(f"設定ファイルの読み込みに失敗しました: {e}", f"Failed to read the config file: {e}"))
        return None

    def save_cached_identity_id(self, identity_id: str):
        """
        取得した Cognito Identity ID を設定ファイル (config.json) に保存します。
        """
        try:
            config_data = {}
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                        config_data = json.load(f)
                except Exception:
                    config_data = {}

            config_data["identity_id"] = identity_id
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config_data, f, indent=4, ensure_ascii=False)
            logger.info(tr(
                f"Cognito Identity ID を {CONFIG_FILE} に保存しました。",
                f"Saved the Cognito Identity ID to {CONFIG_FILE}."
            ))
        except Exception as e:
            logger.warning(tr(f"設定ファイルの保存に失敗しました: {e}", f"Failed to save the config file: {e}"))

    def fetch_cognito_identity_id(self) -> Optional[str]:
        """
        Cognito Identity Pool ID から AWS API 経由で Cognito Identity ID (GetId) を自動取得します。
        """
        if self.cognito_identity_id:
            return self.cognito_identity_id

        url = f"https://cognito-identity.{self.region}.amazonaws.com/"
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityService.GetId"
        }
        payload = {
            "IdentityPoolId": self.identity_pool_id
        }

        logger.info(tr("Cognito Identity ID を取得中...", "Fetching the Cognito Identity ID..."))
        try:
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            res.raise_for_status()
            data = res.json()
            identity_id = data.get("IdentityId")
            if identity_id:
                logger.info(tr(
                    f"Cognito Identity ID の取得成功: {identity_id}",
                    f"Got the Cognito Identity ID: {identity_id}"
                ))
                self.cognito_identity_id = identity_id
                self.save_cached_identity_id(identity_id)
                return identity_id
            else:
                logger.error(tr("レスポンスに IdentityId が含まれていません。", "The response does not contain an IdentityId."))
                return None
        except requests.RequestException as e:
            logger.error(tr(f"Cognito Identity ID の取得に失敗しました: {e}", f"Failed to get the Cognito Identity ID: {e}"))
            return None

    def fetch_cloudfront_cookies(self) -> bool:
        """
        app-signerからCloudFront署名Cookieを取得し、正確な名前でセッションにセットします。
        """
        if not self.cognito_identity_id:
            if not self.fetch_cognito_identity_id():
                return False

        headers = {
            "IdentityId": self.cognito_identity_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; HELLOMovie)"
        }
        payload = {"app_id": "hellomovie"}

        logger.info(tr("CloudFront 署名Cookieを取得中...", "Fetching the CloudFront signed cookies..."))
        try:
            response = requests.post(SIGNER_URL, headers=headers, json=payload, timeout=10)
            response.raise_for_status()

            cf_cookies = {}

            # 1. レスポンスの Cookie (Set-Cookie) から収集
            for cookie in response.cookies:
                c_name_lower = cookie.name.lower()
                if "policy" in c_name_lower:
                    cf_cookies["CloudFront-Policy"] = cookie.value
                elif "signature" in c_name_lower:
                    cf_cookies["CloudFront-Signature"] = cookie.value
                elif "key-pair-id" in c_name_lower or "keypairid" in c_name_lower:
                    cf_cookies["CloudFront-Key-Pair-Id"] = cookie.value

            # 2. レスポンスヘッダーから直接収集
            for h_key, h_val in response.headers.items():
                h_key_lower = h_key.lower()
                if "cloudfront-policy" in h_key_lower or "policy" in h_key_lower:
                    cf_cookies["CloudFront-Policy"] = h_val
                elif "cloudfront-signature" in h_key_lower or "signature" in h_key_lower:
                    cf_cookies["CloudFront-Signature"] = h_val
                elif "cloudfront-key-pair-id" in h_key_lower or "key-pair-id" in h_key_lower:
                    cf_cookies["CloudFront-Key-Pair-Id"] = h_val

            # 3. レスポンスJSON本文から収集
            try:
                res_json = response.json()
                if isinstance(res_json, dict):
                    for k, v in res_json.items():
                        k_lower = str(k).lower()
                        if "policy" in k_lower:
                            cf_cookies["CloudFront-Policy"] = str(v)
                        elif "signature" in k_lower:
                            cf_cookies["CloudFront-Signature"] = str(v)
                        elif "key" in k_lower and "pair" in k_lower:
                            cf_cookies["CloudFront-Key-Pair-Id"] = str(v)
            except Exception:
                pass

            # 抽出したキーを正規名でセッションCookieに登録
            domains = [".hellomovie.app", "app.hellomovie.app"]
            for name, val in cf_cookies.items():
                for domain in domains:
                    self.session.cookies.set(name, val, domain=domain, path="/")

            logger.info(tr(
                f"正規化してセットしたCookieキー一覧: {list(cf_cookies.keys())}",
                f"Cookie keys set (normalized): {list(cf_cookies.keys())}"
            ))
            logger.info(tr("署名Cookieの取得・設定が完了しました。", "Signed cookies fetched and set."))
            return True

        except requests.RequestException as e:
            logger.error(tr(f"署名Cookieの取得に失敗しました: {e}", f"Failed to get the signed cookies: {e}"))
            return False

    def fetch_movies_list(self) -> Optional[Dict[str, Any]]:
        """
        movies.json を取得してDictとして返します。
        """
        url = f"{BASE_URL}/v2/movie/movies.json"
        logger.info(tr(f"作品一覧データを取得中: {url}", f"Fetching the title list: {url}"))

        headers = {
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 11; HELLOMovie)",
            "Accept": "*/*"
        }

        try:
            res = self.session.get(url, headers=headers, timeout=15)
            if res.status_code in (401, 403) and self.fetch_cloudfront_cookies():
                logger.info(tr(
                    "署名Cookieを更新しました。作品一覧を再取得します...",
                    "Signed cookies refreshed. Fetching the title list again..."
                ))
                res = self.session.get(url, headers=headers, timeout=15)
            res.raise_for_status()
            data = res.json()
            count = len(data.get('movies', []))
            logger.info(tr(f"作品一覧の取得完了 ({count} 件)", f"Fetched the title list ({count} titles)"))
            self.save_movies_list(data)
            return data
        except requests.RequestException as e:
            logger.error(tr(f"movies.json の取得に失敗しました: {e}", f"Failed to fetch movies.json: {e}"))
            return None

    def save_movies_list(self, data: Dict[str, Any]):
        """
        取得した movies.json を保存先ディレクトリに保存します (hellomovie_decrypt.py が作品ごとの token を読み取ります)。
        """
        path = os.path.join(self.output_dir, "movies.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
            logger.info(tr(f"作品一覧を {path} に保存しました。", f"Saved the title list to {path}."))
        except OSError as e:
            logger.warning(tr(f"movies.json の保存に失敗しました: {e}", f"Failed to save movies.json: {e}"))

    @staticmethod
    def build_file_url(item: Dict[str, Any], file_type: str) -> Optional[str]:
        """
        作品情報とファイル種別(subtitle/voiceguide/image)からURLを構築します。
        """
        title_id = item.get("title_id")
        external_bucket = item.get("external_bucket", "original")

        if external_bucket == "original":
            prefix = "v2/movie"
        elif external_bucket == "deluxe":
            prefix = "deluxe"
        else:
            prefix = "movie"

        if file_type == "subtitle":
            version = item.get("subtitle_version", 0)
            if version == 0:
                return None
            return f"{BASE_URL}/{prefix}/{title_id}/{version}/subtitles.zip"

        elif file_type == "voiceguide":
            version = item.get("voiceguide_version", 0)
            if version == 0:
                return None
            return f"{BASE_URL}/{prefix}/{title_id}/{version}/voiceguide.zip"

        elif file_type == "image":
            img_name = item.get("image_filename")
            version = item.get("image_version", 0)
            if not img_name or version == 0:
                return None
            return f"{BASE_URL}/{prefix}/{title_id}/{version}/{img_name}"

        return None

    def download_file(self, url: str, filename: str) -> bool:
        """
        指定されたURLからファイルをダウンロードして保存します。
        署名Cookieが失効していた場合 (401/403) は一度だけ再認証してリトライします。
        保存は一時ファイル経由で行うため、途中で失敗しても壊れたファイルは残りません。
        """
        save_path = os.path.join(self.output_dir, filename)
        temp_path = save_path + ".part"
        logger.info(tr(f"ダウンロード開始: {url} -> {save_path}", f"Download started: {url} -> {save_path}"))

        try:
            for attempt in range(2):
                with self.session.get(url, stream=True, timeout=30) as r:
                    if r.status_code in (401, 403) and attempt == 0 and self.fetch_cloudfront_cookies():
                        logger.info(tr(
                            "署名Cookieを更新しました。ダウンロードを再試行します...",
                            "Signed cookies refreshed. Retrying the download..."
                        ))
                        continue
                    r.raise_for_status()
                    with open(temp_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                os.replace(temp_path, save_path)
                logger.info(tr(f"ダウンロード完了: {save_path}", f"Download finished: {save_path}"))
                return True
            logger.error(tr(f"ダウンロードに失敗しました [{url}]: 認証エラー", f"Failed to download [{url}]: authentication error"))
            return False
        except requests.RequestException as e:
            logger.error(tr(f"ファイルのダウンロードに失敗しました [{url}]: {e}", f"Failed to download the file [{url}]: {e}"))
            return False
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def parse_user_selection(self, user_input: str, max_count: int) -> Set[int]:
        """
        ユーザーの多種多様な入力 (例: "1-4", "1 2 3", "1, 3, 5", "all") をインデックスの集合に変換します。
        :return: 0-based index の集合 (Set[int])
        """
        user_input = user_input.strip()
        if not user_input:
            return set()

        if user_input.lower() == "all":
            return set(range(max_count))

        indices = set()
        # カンマやスペースで分割
        tokens = re.split(r'[\s,]+', user_input)

        for token in tokens:
            if not token:
                continue
            # 範囲指定 (例: 1-4)
            if '-' in token:
                parts = token.split('-')
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    start, end = int(parts[0]), int(parts[1])
                    if start > end:
                        start, end = end, start
                    for num in range(start, end + 1):
                        if 1 <= num <= max_count:
                            indices.add(num - 1)
            # 単一の数字 (例: 3)
            elif token.isdigit():
                num = int(token)
                if 1 <= num <= max_count:
                    indices.add(num - 1)

        return indices

    @staticmethod
    def selection_examples() -> str:
        """
        番号選択の入力例を表示用の文字列で返します。
        """
        return tr(
            "\n入力例: '1-4' (範囲), '1 2 3' (複数指定), '1, 3, 5' (複数指定), 'all' (全選択)",
            "\nExamples: '1-4' (range), '1 2 3' (multiple), '1, 3, 5' (multiple), 'all' (select all)"
        )

    def decrypt_downloaded(self, targets: List[Tuple[str, Dict[str, Any]]]):
        """
        ダウンロード済みの ZIP を復号し、"<保存先>/decrypted/<ZIP名>" に保存します。
        :param targets: (ZIPファイル名, 作品情報) のリスト。作品情報の token を鍵の材料に使います。
        """
        succeeded = 0
        for fname, item in targets:
            zip_path = os.path.join(self.output_dir, fname)
            token = int(item.get("token") or 0)
            logger.info(tr(f"復号中: {fname} (token: {token})", f"Decrypting: {fname} (token: {token})"))
            try:
                if hellomovie_decrypt.decrypt_zip(zip_path, hellomovie_decrypt.default_output_dir(zip_path), token):
                    succeeded += 1
            except Exception as e:
                logger.error(tr(f"復号に失敗しました [{fname}]: {e}", f"Decryption failed [{fname}]: {e}"))

        failed = len(targets) - succeeded
        print(tr(f"\n復号結果: 成功 {succeeded} 件 / 失敗 {failed} 件", f"\nDecryption result: {succeeded} succeeded / {failed} failed"))
        out_path = os.path.join(os.path.abspath(self.output_dir), hellomovie_decrypt.DECRYPTED_DIR_NAME)
        print(tr(f"保存先: {out_path}", f"Output folder: {out_path}"))

    def start_decrypt_session(self):
        """
        保存先にあるダウンロード済み ZIP を選んで復号する対話モード (ネットワークは使いません)。
        """
        movies_path = os.path.join(self.output_dir, "movies.json")
        if not os.path.exists(movies_path):
            logger.error(tr(
                f"{movies_path} がありません。先にダウンロードを実行してください (作品一覧が movies.json として保存されます)。",
                f"{movies_path} not found. Run the download first (the title list is saved as movies.json)."
            ))
            return
        try:
            with open(movies_path, "r", encoding="utf-8") as f:
                movies_by_id = {int(m["title_id"]): m for m in json.load(f).get("movies", []) if "title_id" in m}
        except (OSError, ValueError) as e:
            logger.error(tr(f"movies.json の読み込みに失敗しました: {e}", f"Failed to read movies.json: {e}"))
            return

        # (title_id, ファイル名) を title_id 順に並べる
        zip_files: List[Tuple[int, str]] = []
        for name in os.listdir(self.output_dir):
            m = hellomovie_decrypt.ZIP_NAME_RE.match(name)
            if m:
                zip_files.append((int(m.group(1)), name))
        zip_files.sort()
        if not zip_files:
            print(tr(
                f"{self.output_dir} に復号できる ZIP (<ID>_subtitles_v*.zip / <ID>_voiceguide_v*.zip) がありません。",
                f"No decryptable ZIP files (<ID>_subtitles_v*.zip / <ID>_voiceguide_v*.zip) found in {self.output_dir}."
            ))
            return

        entries: List[Tuple[str, Optional[Dict[str, Any]]]] = []
        print("\n" + "="*50)
        print(tr(" HELLOMovie 復号 (対話モード)", " HELLOMovie Decryption (interactive mode)"))
        print("="*50)
        print(tr(f"\n--- ダウンロード済み ZIP ({len(zip_files)} 件) ---", f"\n--- Downloaded ZIP files ({len(zip_files)}) ---"))
        for idx, (title_id, fname) in enumerate(zip_files, start=1):
            item = movies_by_id.get(title_id)
            entries.append((fname, item))
            zip_path = os.path.join(self.output_dir, fname)
            if os.path.isdir(hellomovie_decrypt.default_output_dir(zip_path)):
                status = tr("復号済み", "Decrypted")
            else:
                status = tr("未復号", "Not decrypted")
            title = item.get("name") if item else tr("movies.json に未登録", "not in movies.json")
            print(f"[{idx:3d}] {fname} | {title} | {status}")

        while True:
            print(self.selection_examples())
            choice = input(tr(
                f"復号したい番号を選択してください (1 - {len(entries)} / 'q'で終了): ",
                f"Select the numbers to decrypt (1 - {len(entries)} / 'q' to quit): "
            )).strip()

            if choice.lower() == 'q':
                print(tr("キャンセルしました。", "Cancelled."))
                return

            selected_indices = self.parse_user_selection(choice, len(entries))
            if selected_indices:
                break
            print(tr(
                "無効な入力です。リスト内の番号または正しい形式で入力してください。",
                "Invalid input. Enter numbers from the list in a valid format."
            ))

        targets: List[Tuple[str, Dict[str, Any]]] = []
        for i in sorted(selected_indices):
            fname, item = entries[i]
            if item is None:
                logger.warning(tr(
                    f"{fname} は movies.json に作品情報がないためスキップします。",
                    f"Skipping {fname}: no title information in movies.json."
                ))
                continue
            targets.append((fname, item))
        if targets:
            self.decrypt_downloaded(targets)

    def start_interactive_session(self):
        """
        対話形式で作品・ダウンロード種別を選択し実行するメインループ
        """
        if not self.fetch_cloudfront_cookies():
            logger.error(tr("認証Cookieの取得に失敗したため終了します。", "Exiting because the authentication cookies could not be obtained."))
            return

        movies_data = self.fetch_movies_list()
        if not movies_data:
            return

        movies: List[Dict[str, Any]] = movies_data.get("movies", [])
        if not movies:
            logger.warning(tr("作品リストが空です。", "The title list is empty."))
            return

        print("\n" + "="*50)
        print(tr(" HELLOMovie ダウンローダー (対話モード)", " HELLOMovie Downloader (interactive mode)"))
        print("="*50)

        # 検索キーワードフィルタ
        search_kw = input(tr(
            "\n検索キーワードを入力（そのままEnterで全件表示）: ",
            "\nEnter a search keyword (press Enter to show all): "
        )).strip()
        filtered_movies = movies
        if search_kw:
            filtered_movies = [
                m for m in movies
                if search_kw.lower() in m.get("name", "").lower() or search_kw.lower() in m.get("search_word", "").lower()
            ]

        if not filtered_movies:
            print(tr("条件に一致する作品が見つかりませんでした。", "No titles matched the search."))
            return

        # 一覧表示
        print(tr(f"\n--- 作品一覧 ({len(filtered_movies)} 件) ---", f"\n--- Titles ({len(filtered_movies)}) ---"))
        for idx, item in enumerate(filtered_movies, start=1):
            if item.get("contenttype") == 1:
                ctype_str = tr("字幕", "Subtitles")
            elif item.get("contenttype") == 2:
                ctype_str = tr("音声ガイド", "Audio guide")
            else:
                ctype_str = tr("その他", "Other")
            lang_str = item.get("lang_name", tr("不明", "Unknown"))
            print(tr(
                f"[{idx:3d}] {item.get('name')} | 種別: {ctype_str} | 言語: {lang_str} (ID: {item.get('title_id')})",
                f"[{idx:3d}] {item.get('name')} | Type: {ctype_str} | Language: {lang_str} (ID: {item.get('title_id')})"
            ))

        # 作品の選択 (複数対応)
        selected_indices = set()
        while True:
            print(self.selection_examples())
            choice = input(tr(
                f"ダウンロードしたい作品番号を選択してください (1 - {len(filtered_movies)} / 'q'で終了): ",
                f"Select the title numbers to download (1 - {len(filtered_movies)} / 'q' to quit): "
            )).strip()

            if choice.lower() == 'q':
                print(tr("キャンセルしました。", "Cancelled."))
                return

            selected_indices = self.parse_user_selection(choice, len(filtered_movies))
            if selected_indices:
                break
            print(tr(
                "無効な入力です。リスト内の番号または正しい形式で入力してください。",
                "Invalid input. Enter numbers from the list in a valid format."
            ))

        selected_items = [filtered_movies[i] for i in sorted(selected_indices)]

        # コンテンツ種別の選択
        print(tr(f"\n選択された作品数: {len(selected_items)} 件", f"\nSelected titles: {len(selected_items)}"))
        print(tr("1: コンテンツ本体のみ (字幕ZIP / 音声ガイドZIP)", "1: Content only (subtitle ZIP / audio guide ZIP)"))
        print(tr("2: サムネイル画像のみ", "2: Thumbnail images only"))
        print(tr("3: 両方 (コンテンツ本体 + 画像)", "3: Both (content + images)"))

        while True:
            dl_choice = input(tr(
                "ダウンロードする項目を選択してください (1/2/3, デフォルト: 3): ",
                "Select what to download (1/2/3, default: 3): "
            )).strip()
            if not dl_choice:
                dl_choice = "3"
            if dl_choice in ("1", "2", "3"):
                break
            print(tr("無効な入力です。1, 2, 3 のいずれかを入力してください。", "Invalid input. Enter 1, 2 or 3."))

        print(tr(
            f"\n合計 {len(selected_items)} 件のダウンロードを開始します...\n",
            f"\nStarting the download of {len(selected_items)} title(s)...\n"
        ) + "-"*40)

        # ダウンロードに成功したコンテンツ本体 ZIP (ダウンロード後の復号に使う)
        downloaded_zips: List[Tuple[str, Dict[str, Any]]] = []

        # 連続ダウンロード処理実行
        for idx, selected_item in enumerate(selected_items, start=1):
            t_id = selected_item.get("title_id")
            ctype = selected_item.get("contenttype")
            title_name = selected_item.get("name")

            print(tr(
                f"\n[{idx}/{len(selected_items)}] 処理中: {title_name} (ID: {t_id})",
                f"\n[{idx}/{len(selected_items)}] Processing: {title_name} (ID: {t_id})"
            ))

            # コンテンツ本体
            if dl_choice in ("1", "3"):
                if ctype == 1:
                    url = self.build_file_url(selected_item, "subtitle")
                    if url:
                        fname = f"{t_id}_subtitles_v{selected_item.get('subtitle_version')}.zip"
                        if self.download_file(url, fname):
                            downloaded_zips.append((fname, selected_item))
                elif ctype == 2:
                    url = self.build_file_url(selected_item, "voiceguide")
                    if url:
                        fname = f"{t_id}_voiceguide_v{selected_item.get('voiceguide_version')}.zip"
                        if self.download_file(url, fname):
                            downloaded_zips.append((fname, selected_item))

            # 画像
            if dl_choice in ("2", "3"):
                url = self.build_file_url(selected_item, "image")
                if url:
                    img_name = selected_item.get("image_filename", ".png")
                    ext = os.path.splitext(img_name)[1]
                    fname = f"{t_id}_image_v{selected_item.get('image_version')}{ext}"
                    self.download_file(url, fname)

        print("\n" + "="*50)
        print(tr("すべてのダウンロード処理が完了しました。", "All downloads are complete."))
        print("="*50)

        if downloaded_zips:
            answer = input(tr(
                f"\nダウンロードした {len(downloaded_zips)} 件の ZIP を復号しますか? (y/N): ",
                f"\nDecrypt the {len(downloaded_zips)} downloaded ZIP file(s)? (y/N): "
            )).strip().lower()
            if answer in ("y", "yes"):
                self.decrypt_downloaded(downloaded_zips)


def main(mode: str = "download") -> int:
    """
    モードに応じた対話セッションを実行します。
    :param mode: "download" (ダウンロード) または "decrypt" (復号のみ)
    :return: 終了コード
    """
    if mode not in ("download", "decrypt"):
        print(tr(
            f"不明なモードです: {mode} (download / decrypt のいずれかを指定してください)",
            f"Unknown mode: {mode} (specify download or decrypt)"
        ))
        return 2

    downloader = HelloMovieDownloader(
        region=AWS_REGION,
        identity_pool_id=IDENTITY_POOL_ID,
        output_dir=SAVE_DIRECTORY
    )
    if mode == "download":
        downloader.start_interactive_session()
    else:
        downloader.start_decrypt_session()
    return 0


if __name__ == "__main__":
    # 単体で起動した場合は、設定ファイルに保存された表示言語を使う (run.bat 経由は hellomovie_launcher.py が設定する)
    hellomovie_i18n.init_language(CONFIG_FILE)
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "download"))

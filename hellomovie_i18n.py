import os
import json
import argparse
from typing import Dict, List, Optional

# 対応言語 (コード: 表示名)
SUPPORTED_LANGUAGES: Dict[str, str] = {"ja": "日本語", "en": "English"}
DEFAULT_LANGUAGE = "ja"
CONFIG_FILE = "config.json"

_language = DEFAULT_LANGUAGE


def set_language(lang: str):
    """
    表示言語を切り替えます。以降の tr() の戻り値に反映されます。
    """
    global _language
    if lang not in SUPPORTED_LANGUAGES:
        raise ValueError(f"unsupported language: {lang}")
    _language = lang


def get_language() -> str:
    return _language


def tr(ja: str, en: str) -> str:
    """
    現在の表示言語に合わせて日本語 / 英語の文言を選びます。
    利用者に表示する文言は、すべてこの関数を通します。
    """
    return en if _language == "en" else ja


def load_saved_language(config_path: str = CONFIG_FILE) -> Optional[str]:
    """
    設定ファイル (config.json) に保存された表示言語を返します。未保存・不正な値の場合は None です。
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            lang = json.load(f).get("language")
    except (OSError, ValueError, AttributeError):
        return None
    return lang if isinstance(lang, str) and lang in SUPPORTED_LANGUAGES else None


def save_language(config_path: str, lang: str) -> bool:
    """
    表示言語を設定ファイル (config.json) に保存します。identity_id など他の項目はそのまま残します。
    """
    config_data = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                config_data = loaded
        except (OSError, ValueError):
            config_data = {}

    config_data["language"] = lang
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)
        return True
    except OSError:
        return False


def init_language(config_path: str = CONFIG_FILE, override: Optional[str] = None) -> str:
    """
    起動時の表示言語を決めて設定します (優先順位: 引数の指定 > 設定ファイルの保存値 > 既定値)。
    """
    lang = override if override in SUPPORTED_LANGUAGES else (load_saved_language(config_path) or DEFAULT_LANGUAGE)
    set_language(lang)
    return lang


def preparse_language(argv: Optional[List[str]] = None) -> Optional[str]:
    """
    コマンドライン引数から --lang の値だけを先に取り出します (--help などの文言も同じ言語で出すため)。
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--lang", choices=sorted(SUPPORTED_LANGUAGES))
    known, _ = parser.parse_known_args(argv)
    return known.lang

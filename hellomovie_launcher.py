import sys
import argparse
import subprocess
import importlib
import traceback
from typing import List, Optional

import hellomovie_i18n
from hellomovie_i18n import tr, SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE

# 定数宣言
MAX_INVALID_INPUTS = 5
QUIT_EXIT_CODE = 10  # メニューで終了を選んだときの終了コード (run.bat はこの場合 pause せずに閉じる)
MODES = {"1": "download", "2": "decrypt", "l": "language", "q": "quit"}


def ask(prompt: str) -> Optional[str]:
    """
    1行入力を受け取ります。入力が閉じられている場合 (EOF) は None を返します。
    """
    try:
        return input(prompt).strip()
    except EOFError:
        return None


def choose_language(default: str) -> str:
    """
    表示言語を選択します。言語を決める前の画面なので、この関数だけは日英併記です。
    """
    codes = list(SUPPORTED_LANGUAGES)
    print("\nLanguage / 言語")
    for i, code in enumerate(codes, start=1):
        print(f"  {i}: {SUPPORTED_LANGUAGES[code]}")

    for _ in range(MAX_INVALID_INPUTS):
        answer = ask(f"Select / 選択 [1-{len(codes)}] (Enter = {SUPPORTED_LANGUAGES[default]}): ")
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(codes):
            return codes[int(answer) - 1]
        if answer.lower() in SUPPORTED_LANGUAGES:
            return answer.lower()
        print("Invalid input / 無効な入力です")
    return default


def choose_mode() -> str:
    """
    実行するモード (download / decrypt) か、言語変更 (language)、終了 (quit) を選択します。
    """
    print("\n" + "=" * 50)
    print(tr(" HELLOMovie ツール", " HELLOMovie Tool"))
    print("=" * 50)
    print(tr("  1: ダウンロード", "  1: Download"))
    print(tr("  2: ダウンロード済みファイルの復号", "  2: Decrypt downloaded files"))
    print(tr("  L: 言語を変更", "  L: Change language"))
    print(tr("  Q: 終了", "  Q: Quit"))

    for _ in range(MAX_INVALID_INPUTS):
        answer = ask(tr("選択してください [1/2/L/Q]: ", "Select [1/2/L/Q]: "))
        if answer is None:
            return "quit"
        mode = MODES.get(answer.lower())
        if mode:
            return mode
        print(tr("無効な入力です。1, 2, L, Q のいずれかを入力してください。", "Invalid input. Enter 1, 2, L or Q."))

    print(tr("無効な入力が続いたため終了します。", "Too many invalid inputs. Exiting."))
    return "quit"


def ensure_requests() -> bool:
    """
    ダウンローダーが使う requests が無ければ pip でインストールします。
    """
    try:
        import requests  # noqa: F401
        return True
    except ImportError:
        pass

    print(tr("requests パッケージをインストールしています...", "Installing the requests package..."))
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "requests"])
    importlib.invalidate_caches()
    try:
        import requests  # noqa: F401
        return True
    except ImportError:
        print(tr("[エラー] requests のインストールに失敗しました。", "[ERROR] Failed to install requests."))
        return False


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # 表示言語の決定: --lang > 設定ファイルの保存値 > (初回のみ) 選択画面
    saved = hellomovie_i18n.load_saved_language()
    lang = hellomovie_i18n.preparse_language(argv) or saved or choose_language(DEFAULT_LANGUAGE)
    hellomovie_i18n.set_language(lang)
    if lang != saved:
        hellomovie_i18n.save_language(hellomovie_i18n.CONFIG_FILE, lang)

    parser = argparse.ArgumentParser(
        description=tr("HELLOMovie のダウンロード / 復号ツールの起動スクリプト。", "Launcher for the HELLOMovie download / decrypt tool.")
    )
    parser.add_argument("--lang", choices=sorted(SUPPORTED_LANGUAGES),
                        help=tr("表示言語 (次回以降も保存されます)", "Display language (remembered for next time)"))
    parser.add_argument("--mode", choices=["download", "decrypt"],
                        help=tr("メニューを出さずにモードを直接指定", "Run a mode directly without showing the menu"))
    args = parser.parse_args(argv)

    mode = args.mode
    while mode is None:
        choice = choose_mode()
        if choice == "language":
            lang = choose_language(lang)
            hellomovie_i18n.set_language(lang)
            hellomovie_i18n.save_language(hellomovie_i18n.CONFIG_FILE, lang)
            continue
        mode = choice
    if mode == "quit":
        return QUIT_EXIT_CODE

    if not ensure_requests():
        return 1

    try:
        import hellomovie_downloader
        exit_code = hellomovie_downloader.main(mode)
    except KeyboardInterrupt:
        print(tr("\n中断しました。", "\nInterrupted."))
        return 130
    except EOFError:
        print(tr("\n入力が終了したため中断しました。", "\nInput ended; aborting."))
        return 1
    except Exception:
        traceback.print_exc()
        print(tr("予期しないエラーが発生しました。", "An unexpected error occurred."))
        return 1

    if exit_code == 0:
        print(tr("完了しました。", "Finished."))
    else:
        print(tr(f"エラーで終了しました (終了コード {exit_code})。", f"Exited with an error (exit code {exit_code})."))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

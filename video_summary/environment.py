# 実行環境のチェック (ffmpeg / GPU / CUDA / Ollama)
#
# CUDA Toolkit を WSL に入れなくても動くように、pip でインストールした
# nvidia-cublas-cu12 / nvidia-cudnn-cu12 の共有ライブラリを事前ロードして
# CTranslate2 (faster-whisper) から見えるようにしている。
import ctypes
import glob
import os
import shutil
import subprocess

import requests

from .utils import VideoSummaryError, log, warn

_cuda_preloaded = False


def preload_cuda_libraries() -> None:
    """pip の nvidia-* パッケージ内の .so を RTLD_GLOBAL でロードする。

    dlopen 済みの soname は後続の dlopen で再利用されるため、これで
    LD_LIBRARY_PATH を設定しなくても cuBLAS / cuDNN が解決できる。
    """
    global _cuda_preloaded
    if _cuda_preloaded:
        return
    _cuda_preloaded = True
    try:
        import nvidia
    except ImportError:
        return
    lib_paths: list[str] = []
    for base in nvidia.__path__:
        lib_paths.extend(glob.glob(os.path.join(base, "*", "lib", "*.so*")))
    # 依存順の前後で失敗することがあるため 2 周してリトライする
    for _ in range(2):
        for path in sorted(lib_paths):
            try:
                ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                pass


def check_command(name: str, hint: str) -> None:
    if shutil.which(name) is None:
        raise VideoSummaryError(f"{name} が見つかりません。{hint}")


def check_ffmpeg() -> None:
    check_command(
        "ffmpeg",
        "インストールしてください: sudo apt update && sudo apt install -y ffmpeg",
    )
    check_command(
        "ffprobe",
        "ffprobe は ffmpeg に同梱されています: sudo apt install -y ffmpeg",
    )


def nvidia_smi_available() -> bool:
    if shutil.which("nvidia-smi") is None:
        return False
    try:
        result = subprocess.run(
            ["nvidia-smi"], capture_output=True, timeout=15
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def cuda_device_count() -> int:
    """CTranslate2 から見える CUDA デバイス数 (0 なら GPU 利用不可)"""
    preload_cuda_libraries()
    try:
        import ctranslate2
    except ImportError:
        raise VideoSummaryError(
            "faster-whisper (ctranslate2) がインストールされていません。\n"
            "  source .venv/bin/activate && pip install -r requirements.txt"
        )
    try:
        return ctranslate2.get_cuda_device_count()
    except Exception:
        return 0


def check_ollama(url: str) -> str:
    """Ollama サーバーの疎通確認。バージョン文字列を返す"""
    try:
        res = requests.get(f"{url}/api/version", timeout=5)
        res.raise_for_status()
        return res.json().get("version", "unknown")
    except requests.RequestException as e:
        raise VideoSummaryError(
            f"Ollama サーバーに接続できません ({url})。\n"
            f"  起動していない場合は別ターミナルで `ollama serve` を実行するか、\n"
            f"  systemd 環境なら `sudo systemctl start ollama` を実行してください。\n"
            f"  詳細: {e}"
        )


def check_ollama_model(url: str, model: str) -> None:
    """指定モデルが取得済みかを確認する"""
    try:
        res = requests.get(f"{url}/api/tags", timeout=10)
        res.raise_for_status()
        models = res.json().get("models", [])
    except requests.RequestException as e:
        raise VideoSummaryError(f"Ollama のモデル一覧を取得できません: {e}")
    names = {m.get("name", "") for m in models}
    # "qwen3:8b" のほか、タグ省略時 ("qwen3" → "qwen3:latest") も許容する
    candidates = {model, f"{model}:latest"}
    if not (candidates & names):
        available = ", ".join(sorted(names)) or "(なし)"
        raise VideoSummaryError(
            f"Ollama モデル '{model}' が見つかりません。\n"
            f"  取得するには: ollama pull {model}\n"
            f"  取得済みモデル: {available}"
        )


def check_environment(config, need_ollama: bool = True) -> None:
    """パイプライン実行前の環境チェック。config の実行時フィールドを更新する"""
    from . import video  # 循環 import 回避のため遅延 import

    check_ffmpeg()

    info = video.probe_video(config.input_path)
    if not info["has_video"]:
        raise VideoSummaryError(
            f"入力ファイルに映像トラックがありません: {config.input_path}\n"
            "  音声のみのファイルからダイジェスト動画は生成できません。"
        )
    if not info["has_audio"]:
        raise VideoSummaryError(
            f"入力動画に音声トラックがありません: {config.input_path}\n"
            "  本ツールは音声の文字起こしを前提としています。"
        )
    config.video_duration = info["duration"]
    log(f"  入力動画: {config.input_path.name} "
        f"({info['duration']:.1f} 秒, 音声トラックあり)")

    if config.video_duration <= config.target_seconds:
        warn(
            f"動画の長さ ({config.video_duration:.0f} 秒) が目標時間 "
            f"({config.target_seconds:.0f} 秒) 以下です。要約の意味がない可能性があります。"
        )

    # GPU / CUDA
    if config.force_cpu:
        config.use_cuda = False
        log("  --force-cpu 指定のため CPU で文字起こしします")
    else:
        if not nvidia_smi_available():
            warn(
                "nvidia-smi が実行できません。WSL から GPU が見えていない可能性があります。\n"
                "  Windows 側の NVIDIA ドライバを更新し、`wsl --update` を試してください。\n"
                "  CPU モードで続行します (大幅に時間がかかります)。"
            )
            config.use_cuda = False
        elif cuda_device_count() == 0:
            warn(
                "CUDA デバイスが faster-whisper (CTranslate2) から利用できません。\n"
                "  pip install -r requirements.txt で nvidia-cublas-cu12 / "
                "nvidia-cudnn-cu12 が入っているか確認してください。\n"
                "  CPU モードで続行します (大幅に時間がかかります)。"
            )
            config.use_cuda = False
        else:
            config.use_cuda = True
            log("  CUDA: 利用可能 (faster-whisper は GPU で実行)")

    # NVENC
    config.use_nvenc = video.detect_nvenc()
    if config.use_nvenc:
        log("  NVENC: 利用可能 (h264_nvenc でエンコード)")
    else:
        log("  NVENC: 利用不可 (libx264 にフォールバック)")

    # Ollama (採点結果を再利用できる場合はチェック不要)
    if not need_ollama:
        log("  Ollama: チェックをスキップ (既存の採点結果を再利用)")
    else:
        version = check_ollama(config.ollama_url)
        check_ollama_model(config.ollama_url, config.llm_model)
        log(f"  Ollama: v{version}, モデル '{config.llm_model}' 確認済み")

# FFmpeg / ffprobe による動画処理 (プローブ・NVENC 判定・切り出し・結合)
import json
import shutil
import subprocess
from pathlib import Path

from .utils import VideoSummaryError, log, warn

_nvenc_available: bool | None = None


def _run(cmd: list[str], error_label: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise VideoSummaryError(f"{cmd[0]} が見つかりません。インストールしてください。")
    except subprocess.TimeoutExpired:
        raise VideoSummaryError(f"{error_label} がタイムアウトしました: {' '.join(cmd[:6])} ...")
    if result.returncode != 0:
        stderr_tail = "\n".join(result.stderr.strip().splitlines()[-10:])
        raise VideoSummaryError(f"{error_label} に失敗しました:\n{stderr_tail}")
    return result


def probe_video(path: Path) -> dict:
    """動画の長さと音声トラック有無を取得する"""
    result = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-show_entries", "stream=codec_type",
            "-of", "json", str(path),
        ],
        f"動画情報の取得 ({path.name})",
        timeout=60,
    )
    try:
        data = json.loads(result.stdout)
        duration = float(data["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError):
        raise VideoSummaryError(
            f"動画情報を解析できません。動画ファイルが壊れている可能性があります: {path}"
        )
    codec_types = {s.get("codec_type") for s in data.get("streams", [])}
    return {
        "duration": duration,
        "has_audio": "audio" in codec_types,
        "has_video": "video" in codec_types,
    }


def detect_nvenc() -> bool:
    """h264_nvenc が実際に使えるか、1 フレームのテストエンコードで確認する"""
    global _nvenc_available
    if _nvenc_available is not None:
        return _nvenc_available
    if shutil.which("ffmpeg") is None:
        _nvenc_available = False
        return False
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
                "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            capture_output=True, timeout=30,
        )
        _nvenc_available = result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        _nvenc_available = False
    return _nvenc_available


def _video_encode_args(use_nvenc: bool) -> list[str]:
    # -pix_fmt yuv420p: 10-bit HDR 入力 (スマホの HEVC Main 10 等) を
    # そのまま渡すと h264_nvenc が CreateInputBuffer failed で失敗するため、
    # 8-bit へ明示的に変換する (8-bit 入力には影響しない)
    if use_nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
                "-cq", "23", "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-pix_fmt", "yuv420p"]


def _cut_one(input_path: Path, start: float, duration: float,
             out_path: Path, use_nvenc: bool) -> None:
    # -ss を -i の前に置く入力シーク。再エンコードするためフレーム精度で切れる。
    # 音声も必ず再エンコードして全クリップのパラメータを揃え、結合時の
    # 音ズレ・無音化を防ぐ。
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(input_path),
        "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "0:a:0",
        *_video_encode_args(use_nvenc),
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(cmd, f"クリップの切り出し ({out_path.name})")


def cut_clips(input_path: Path, intervals: list[dict], clips_dir: Path,
              use_nvenc: bool) -> list[Path]:
    """選択区間を切り出して output/clips/001.mp4 ... として保存する。

    全クリップは必ず同一エンコーダで書き出す。エンコーダが混在すると
    パラメータセットの異なる H.264 を -c copy で結合することになり、
    規格違反の MP4 が生成されるため、途中で NVENC が失敗した場合
    (セッション上限等) は最初から全クリップを libx264 でやり直す。
    """
    encoders = [True, False] if use_nvenc else [False]
    for nvenc in encoders:
        try:
            return _cut_all(input_path, intervals, clips_dir, nvenc)
        except VideoSummaryError:
            if not nvenc:
                raise
            warn("h264_nvenc でのエンコードに失敗したため、"
                 "全クリップを libx264 で切り出し直します")
    raise AssertionError("unreachable")


def _cut_all(input_path: Path, intervals: list[dict], clips_dir: Path,
             use_nvenc: bool) -> list[Path]:
    if clips_dir.exists():
        shutil.rmtree(clips_dir)  # 前回の残骸が混ざらないよう作り直す
    clips_dir.mkdir(parents=True)

    clip_paths = []
    for i, iv in enumerate(intervals, start=1):
        out_path = clips_dir / f"{i:03d}.mp4"
        duration = iv["end"] - iv["start"]
        log(f"  Cutting clip {i}/{len(intervals)} "
            f"({iv['start']:.1f}s - {iv['end']:.1f}s, {duration:.1f}s)")
        _cut_one(input_path, iv["start"], duration, out_path, use_nvenc)
        clip_paths.append(out_path)
    return clip_paths


def concat_clips(clip_paths: list[Path], output_path: Path) -> None:
    """concat demuxer でクリップを結合する。

    全クリップを同一パラメータで再エンコード済みのため -c copy で安全に
    結合でき、再エンコードによる劣化も音ズレも起きない。
    """
    list_path = output_path.parent / "concat_list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for p in clip_paths:
            # concat demuxer のエスケープ規則: シングルクォートは '\'' にする
            escaped = str(p.resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
    try:
        _run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(list_path),
                "-c", "copy", "-movflags", "+faststart",
                str(output_path),
            ],
            "クリップの結合",
        )
    finally:
        list_path.unlink(missing_ok=True)


def render_summary(config, intervals: list[dict]) -> Path:
    """切り出しと結合を実行し、要約動画のパスを返す"""
    if not intervals:
        raise VideoSummaryError("選択された区間がありません。要約動画を生成できません。")
    clip_paths = cut_clips(
        config.input_path, intervals, config.clips_dir, config.use_nvenc
    )
    concat_clips(clip_paths, config.summary_path)
    if not config.keep_clips:
        shutil.rmtree(config.clips_dir)
    return config.summary_path

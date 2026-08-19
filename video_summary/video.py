# FFmpeg / ffprobe による動画処理 (プローブ・NVENC 判定・切り出し・結合)
import json
import shutil
import subprocess
import tempfile
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
            "-show_entries", "stream=codec_type,duration",
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
    # 映像ストリーム自体の長さ。音声だけが後ろへ伸びるファイルではコンテナ長より
    # 短くなり、映像のない位置を切り出さないための基準になる (TS 等で取得できない
    # 場合はコンテナ長へフォールバック)
    video_duration = duration
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            try:
                video_duration = float(s["duration"])
            except (KeyError, TypeError, ValueError):
                pass
            break
    return {
        "duration": duration,
        "video_duration": video_duration,
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
             out_path: Path, use_nvenc: bool, with_audio: bool,
             force_stereo: bool = False) -> None:
    # -ss を -i の前に置く入力シーク。再エンコードするためフレーム精度で切れる。
    # 音声も必ず再エンコードして全クリップのパラメータを揃え、結合時の
    # 音ズレ・無音化を防ぐ。
    if with_audio:
        audio_args = ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k",
                      "-ar", "48000"]
        if force_stereo:
            audio_args += ["-ac", "2"]
    else:
        audio_args = ["-an"]
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(input_path),
        "-t", f"{duration:.3f}",
        "-map", "0:v:0", *audio_args,
        *_video_encode_args(use_nvenc),
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run(cmd, f"クリップの切り出し ({out_path.name})")


def cut_clips(input_path: Path, intervals: list[dict], clips_dir: Path,
              use_nvenc: bool, with_audio: bool = True) -> list[Path]:
    """選択区間を切り出して output/clips/001.mp4 ... として保存する。

    全クリップは必ず同一エンコーダで書き出す。エンコーダが混在すると
    パラメータセットの異なる H.264 を -c copy で結合することになり、
    規格違反の MP4 が生成されるため、途中で NVENC が失敗した場合
    (セッション上限等) は最初から全クリップを libx264 でやり直す。
    """
    encoders = [True, False] if use_nvenc else [False]
    for nvenc in encoders:
        try:
            clip_paths = _cut_all(input_path, intervals, clips_dir, nvenc,
                                  with_audio)
            if with_audio:
                _ensure_audio_consistency(input_path, intervals, clip_paths,
                                          nvenc)
            return clip_paths
        except VideoSummaryError:
            if not nvenc:
                raise
            warn("h264_nvenc でのエンコードに失敗したため、"
                 "全クリップを libx264 で切り出し直します")
    raise AssertionError("unreachable")


def _probe_clip_audio(path: Path) -> tuple | None:
    """先頭音声ストリームの構成 (channels, layout, sample_rate) を返す。

    音声ストリーム自体がなければ None。クリップ間の構成比較に使う。
    """
    result = _run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=channels,channel_layout,sample_rate",
            "-of", "json", str(path),
        ],
        f"音声情報の取得 ({path.name})",
        timeout=60,
    )
    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError:
        raise VideoSummaryError(f"音声情報を解析できません: {path}")
    if not streams:
        return None
    s = streams[0]
    return (s.get("channels"), s.get("channel_layout", ""),
            s.get("sample_rate"))


def _probe_audio_layout(path: Path) -> str:
    """入力音声のチャンネルレイアウト文字列を返す (不明時は stereo)。

    無音クリップのレイアウトを実クリップと一致させるために使う。レイアウト
    不明の音声は FFmpeg がチャンネル数既定のレイアウトを補うため、それに
    合わせて "Nc" (N チャンネル既定レイアウト) を返す。ffprobe は不明時に
    channels=0 を返すことがあるので、その場合もステレオへ倒す。
    """
    try:
        audio = _probe_clip_audio(path)
    except VideoSummaryError:
        audio = None
    if audio is None:
        return "stereo"
    channels, layout, _ = audio
    if isinstance(layout, str) and layout and layout != "unknown":
        return layout
    try:
        n = int(channels)
    except (TypeError, ValueError):
        n = 0
    return f"{n}c" if n > 0 else "stereo"


def _cut_one_silent(input_path: Path, start: float, duration: float,
                    out_path: Path, use_nvenc: bool, layout: str) -> None:
    """音声パケットのない区間を、無音音声付きで切り出し直す。

    無音のチャンネルレイアウトは実クリップ (入力の復号結果) と一致させる。
    構成が食い違うと concat -c copy 後の AAC が復号できなくなる。
    """
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(input_path),
        "-f", "lavfi", "-i", f"anullsrc=r=48000:cl={layout}",
        "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-map", "1:a:0",
        *_video_encode_args(use_nvenc),
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-shortest", "-movflags", "+faststart",
        str(out_path),
    ]
    _run(cmd, f"クリップの切り出し ({out_path.name})")


def _ensure_audio_consistency(input_path: Path, intervals: list[dict],
                              clip_paths: list[Path],
                              use_nvenc: bool) -> None:
    """全クリップの音声ストリーム構成を揃える。

    concat (-c copy) は先頭クリップの構成でコンテナを作るため、構成の
    混在したクリップを渡すと残りのクリップの音声が無警告で壊れる。
    - 音声のないクリップ (音声トラックが全編をカバーしない入力):
      入力と同じレイアウトの無音音声を挿入する
    - チャンネル構成の混在 (途中でステレオと 5.1 が切り替わる入力等):
      全クリップの音声をステレオへ統一する
    """
    audios = [_probe_clip_audio(p) for p in clip_paths]
    if all(a is None for a in audios):
        warn("どのクリップ位置にも音声パケットがないため、音声なしで出力します")
        return
    silent_indices = [i for i, a in enumerate(audios) if a is None]
    if silent_indices:
        warn("音声トラックが動画全編をカバーしていないため、"
             "音声のない区間には無音を挿入します")
        layout = _probe_audio_layout(input_path)
        for i in silent_indices:
            iv = intervals[i]
            _cut_one_silent(input_path, iv["start"], iv["end"] - iv["start"],
                            clip_paths[i], use_nvenc, layout)
            audios[i] = _probe_clip_audio(clip_paths[i])
    if len(set(audios)) > 1:
        warn("クリップ間で音声のチャンネル構成が一致しないため、"
             "全クリップの音声をステレオへ統一します")
        silent = set(silent_indices)
        for i, iv in enumerate(intervals):
            duration = iv["end"] - iv["start"]
            if i in silent:
                _cut_one_silent(input_path, iv["start"], duration,
                                clip_paths[i], use_nvenc, "stereo")
            else:
                _cut_one(input_path, iv["start"], duration, clip_paths[i],
                         use_nvenc, with_audio=True, force_stereo=True)


def _cut_all(input_path: Path, intervals: list[dict], clips_dir: Path,
             use_nvenc: bool, with_audio: bool) -> list[Path]:
    if clips_dir.exists():
        shutil.rmtree(clips_dir)  # 前回の残骸が混ざらないよう作り直す
    clips_dir.mkdir(parents=True)

    clip_paths = []
    for i, iv in enumerate(intervals, start=1):
        out_path = clips_dir / f"{i:03d}.mp4"
        duration = iv["end"] - iv["start"]
        log(f"  Cutting clip {i}/{len(intervals)} "
            f"({iv['start']:.1f}s - {iv['end']:.1f}s, {duration:.1f}s)")
        _cut_one(input_path, iv["start"], duration, out_path, use_nvenc,
                 with_audio)
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
        config.input_path, intervals, config.clips_dir, config.use_nvenc,
        with_audio=config.has_audio,
    )
    # 映像のないクリップが混ざると concat のストリーム対応がずれ、
    # 再生不能なファイルが「Done」のまま生成されてしまうため事前に検査する
    broken = [p.name for p in clip_paths if not probe_video(p)["has_video"]]
    if broken:
        raise VideoSummaryError(
            f"映像フレームのないクリップが生成されました: {', '.join(broken)}\n"
            "  入力動画の映像ストリームに欠損がある可能性があります。"
        )
    concat_clips(clip_paths, config.summary_path)
    if not config.keep_clips:
        shutil.rmtree(config.clips_dir)
    return config.summary_path


def render_timelapse(config) -> Path:
    """全編を倍速圧縮したタイムラプスを生成する (--mode timelapse)。

    音声は倍速では意味をなさないため出力しない。全編の再エンコードに
    なるため、-progress の出力を読んで進捗を表示する。
    """
    factor = config.video_duration / config.target_seconds
    log(f"  {factor:.1f} 倍速に圧縮します (全編を再エンコードするため時間がかかります)")
    encoders = [True, False] if config.use_nvenc else [False]
    for nvenc in encoders:
        try:
            _encode_timelapse(config, factor, nvenc)
            break
        except VideoSummaryError as e:
            # NVENC 起因と判定できた失敗のみ libx264 でやり直す。シグナル停止や
            # ディスク・権限エラーまで NVENC のせいにすると、誤った診断のまま
            # 全編をもう一度エンコードしてしまう
            if not nvenc or not getattr(e, "nvenc_failure", False):
                raise
            warn("h264_nvenc でのエンコードに失敗したため、libx264 でやり直します")
    # フィルタ結果が 0 フレームでも ffmpeg は正常終了するため、
    # 空の MP4 を「Done」として返さないよう出力を検証する。
    # ストリームが 1 本もない場合は probe 自体が失敗するため、それも同扱いにする
    try:
        info = probe_video(config.summary_path)
        valid = info["has_video"] and info["duration"] > 0
    except VideoSummaryError:
        valid = False
    if not valid:
        raise VideoSummaryError(
            "タイムラプスの出力に映像フレームがありません。\n"
            "  --minutes が短すぎて 1 フレームも生成されなかった可能性があります。"
        )
    return config.summary_path


def _encode_timelapse(config, factor: float, use_nvenc: bool) -> None:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
        "-i", str(config.input_path),
        "-vf", f"setpts=PTS/{factor:.6f},fps=30",
        "-an",
        *_video_encode_args(use_nvenc),
        "-movflags", "+faststart",
        "-progress", "pipe:1",
        str(config.summary_path),
    ]
    # stderr をパイプにすると進捗読み取り中に詰まる可能性があるため一時ファイルへ
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                errors="replace") as errf:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                    text=True)
        except OSError as e:
            raise VideoSummaryError(f"ffmpeg を起動できません: {e}")
        try:
            next_pct = 10
            for line in proc.stdout:
                # out_time_ms はマイクロ秒 (ffmpeg の歴史的経緯によるフィールド名)
                if not line.startswith("out_time_ms="):
                    continue
                try:
                    out_sec = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                pct = out_sec / config.target_seconds * 100
                if pct >= next_pct:
                    log(f"  {min(pct, 100):.0f}%")
                    while next_pct <= pct:
                        next_pct += 10
            proc.wait()
        finally:
            # Ctrl+C や出力先パイプの切断で抜けた場合も ffmpeg を残さない
            proc.stdout.close()
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        if proc.returncode != 0:
            errf.seek(0)
            stderr_text = errf.read().strip()
            stderr_tail = "\n".join(stderr_text.splitlines()[-10:])
            err = VideoSummaryError(
                f"タイムラプスの生成に失敗しました "
                f"(returncode={proc.returncode}):\n{stderr_tail}")
            # 負値はシグナルによる強制終了、255 は ffmpeg 自身のシグナル捕捉。
            # それ以外でエンコーダ関連の出力があるときだけ NVENC 起因とみなす
            lower = stderr_text.lower()
            err.nvenc_failure = (
                proc.returncode > 0 and proc.returncode != 255
                and ("nvenc" in lower or "cuda" in lower or "cuvid" in lower)
            )
            raise err

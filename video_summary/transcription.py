# faster-whisper による文字起こし
#
# 長時間動画対策: faster-whisper にファイルを直接渡すと全音声と全長の
# メルスペクトログラムを一括でメモリに載せる (2 時間で数 GB)。そのため
# 15 分を超える動画は無音位置を目安に約 10 分のチャンクへ分割し、
# チャンクごとに転写してタイムスタンプをオフセットする。
# チャンク完了ごとに部分結果を保存するため、中断しても --resume で
# 続きから再開できる。
import re
import subprocess

from .environment import preload_cuda_libraries
from .utils import (
    VideoSummaryError,
    format_timestamp,
    log,
    read_json,
    warn,
    write_json_atomic,
)

CHUNK_SECONDS = 600.0      # チャンクの目安長
SINGLE_PASS_LIMIT = 900.0  # これ以下の動画は分割しない
BOUNDARY_SEARCH = 90.0     # チャンク境界を探す範囲 (目安位置から±秒)
PROGRESS_EVERY = 50        # 何セグメントごとに進捗を表示するか


class _ModelState:
    """Whisper モデルと実行デバイスを保持し、CPU フォールバックを一元管理する"""

    def __init__(self, model_name: str, use_cuda: bool):
        preload_cuda_libraries()
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise VideoSummaryError(
                "faster-whisper がインストールされていません。\n"
                "  source .venv/bin/activate && pip install -r requirements.txt"
            )
        self._cls = WhisperModel
        self.name = model_name
        self.device = "cpu"
        self.model = None

        if use_cuda:
            try:
                self.model = WhisperModel(model_name, device="cuda",
                                          compute_type="float16")
                self.device = "cuda"
            except Exception as e:
                warn(
                    f"GPU でのモデルロードに失敗しました ({e})。\n"
                    f"  VRAM 不足の場合は --whisper-model turbo / medium / small を試してください。\n"
                    f"  CPU モードにフォールバックします (大幅に時間がかかります)。"
                )
        if self.model is None:
            self._load_cpu()

    def _load_cpu(self) -> None:
        try:
            self.model = self._cls(self.name, device="cpu", compute_type="int8")
            self.device = "cpu"
        except Exception as e:
            raise VideoSummaryError(
                f"Whisper モデル '{self.name}' をロードできません: {e}")

    def fallback_to_cpu(self) -> None:
        self.model = None
        self._load_cpu()


def _detect_silences(path) -> list[tuple[float, float]]:
    """ffmpeg の silencedetect で無音区間 (start, end) の一覧を得る"""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
                "-af", "silencedetect=noise=-30dB:d=0.6", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=1800,
        )
    except (subprocess.SubprocessError, OSError) as e:
        warn(f"無音解析に失敗しました (固定位置で分割します): {e}")
        return []
    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", result.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", result.stderr)]
    return list(zip(starts, ends))


def _chunk_boundaries(duration: float,
                      silences: list[tuple[float, float]]) -> list[float]:
    """発話の途中で切らないよう、無音の中央を優先してチャンク境界を決める"""
    bounds = [0.0]
    t = CHUNK_SECONDS
    while t < duration - CHUNK_SECONDS * 0.25:
        best = None
        for s, e in silences:
            mid = (s + e) / 2
            if abs(mid - t) <= BOUNDARY_SEARCH and (
                    best is None or abs(mid - t) < abs(best - t)):
                best = mid
        boundary = best if best is not None else t
        if boundary - bounds[-1] >= 60.0:
            bounds.append(round(boundary, 3))
        t += CHUNK_SECONDS
    bounds.append(round(duration, 3))
    return bounds


def _read_audio_chunk(path, start: float, duration: float):
    """指定区間を 16kHz モノラル float32 で読み出す (チャンクあたり数十 MB)"""
    import numpy as np

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-ss", f"{start:.3f}", "-i", str(path),
                "-t", f"{duration:.3f}", "-vn",
                "-ac", "1", "-ar", "16000", "-f", "f32le", "pipe:1",
            ],
            capture_output=True, timeout=1800,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise VideoSummaryError(f"音声の読み出しに失敗しました: {e}")
    if result.returncode != 0:
        stderr_tail = result.stderr.decode(errors="replace").strip()[-500:]
        raise VideoSummaryError(f"音声の読み出しに失敗しました:\n{stderr_tail}")
    return np.frombuffer(result.stdout, dtype=np.float32)


def _run_transcribe(state: _ModelState, source, offset: float,
                    total: float, config,
                    progress_base: int) -> tuple[list[dict], str]:
    """1 つのソース (ファイルパスまたは音声配列) を転写する"""
    segments_iter, info = state.model.transcribe(
        source,
        language=config.language,
        vad_filter=True,
        beam_size=5,
    )
    segments = []
    count = progress_base
    for seg in segments_iter:
        text = seg.text.strip()
        if not text:
            continue
        segments.append({
            "start": round(seg.start + offset, 3),
            "end": round(seg.end + offset, 3),
            "text": text,
        })
        count += 1
        if count % PROGRESS_EVERY == 0 and total > 0:
            abs_end = seg.end + offset
            pct = min(abs_end / total * 100, 100.0)
            log(f"  {format_timestamp(abs_end)} / "
                f"{format_timestamp(total)} ({pct:.0f}%)")
    return segments, info.language


def _transcribe_with_fallback(state: _ModelState, source, offset: float,
                              total: float, config,
                              progress_base: int) -> tuple[list[dict], str]:
    """転写を実行し、GPU で失敗した場合は CPU で 1 回やり直す"""
    try:
        return _run_transcribe(state, source, offset, total, config,
                               progress_base)
    except Exception as e:
        if state.device != "cuda":
            raise VideoSummaryError(f"文字起こし中にエラーが発生しました: {e}")
        warn(
            f"GPU での文字起こしに失敗しました ({e})。\n"
            f"  CPU に切り替えて再試行します (大幅に時間がかかります)。"
        )
        state.fallback_to_cpu()
        try:
            return _run_transcribe(state, source, offset, total, config,
                                   progress_base)
        except Exception as e2:
            raise VideoSummaryError(f"文字起こし中にエラーが発生しました: {e2}")


def transcribe(config) -> dict:
    """文字起こしを実行し transcript.json / transcript.txt を保存する"""
    state = _ModelState(config.whisper_model, config.use_cuda)
    log(f"  モデル: {config.whisper_model} ({state.device})")

    duration = config.video_duration
    if duration <= SINGLE_PASS_LIMIT:
        segments, language = _transcribe_with_fallback(
            state, str(config.input_path), 0.0, duration, config, 0)
    else:
        segments, language = _transcribe_chunked(state, config, duration)

    if not segments:
        raise VideoSummaryError(
            "文字起こし結果が空です。音声に発話が含まれていないか、"
            "言語設定 (--language) が合っていない可能性があります。"
        )

    result = {
        "language": language or config.language,
        "duration": round(duration, 3),
        "segments": segments,
    }
    write_json_atomic(config.transcript_json, result)
    _write_transcript_txt(config.transcript_txt, segments)
    config.transcript_partial_json.unlink(missing_ok=True)
    return result


def _transcribe_chunked(state: _ModelState, config,
                        duration: float) -> tuple[list[dict], str]:
    log("  無音位置を解析してチャンク境界を決定中...")
    silences = _detect_silences(config.input_path)
    bounds = _chunk_boundaries(duration, silences)
    n_chunks = len(bounds) - 1

    partial_params = {
        "whisper_model": config.whisper_model,
        "language": config.language,
        "boundaries": bounds,
    }

    # 部分結果の再開 (--resume 時のみ)
    segments: list[dict] = []
    start_chunk = 0
    language = ""
    if config.resume and config.transcript_partial_json.exists():
        try:
            partial = read_json(config.transcript_partial_json)
            if (isinstance(partial, dict)
                    and partial.get("params") == partial_params):
                segments = partial.get("segments", [])
                start_chunk = int(partial.get("done", 0))
                language = partial.get("language", "")
                log(f"  部分的な文字起こし結果を再利用: "
                    f"チャンク {start_chunk}/{n_chunks} まで完了済み")
        except (ValueError, OSError):
            pass

    for i in range(start_chunk, n_chunks):
        chunk_start, chunk_end = bounds[i], bounds[i + 1]
        log(f"  チャンク {i + 1}/{n_chunks} "
            f"({format_timestamp(chunk_start)} - {format_timestamp(chunk_end)})")
        audio = _read_audio_chunk(config.input_path, chunk_start,
                                  chunk_end - chunk_start)
        chunk_segments, detected = _transcribe_with_fallback(
            state, audio, chunk_start, duration, config, len(segments))
        del audio
        segments.extend(chunk_segments)
        language = language or detected
        write_json_atomic(config.transcript_partial_json, {
            "params": partial_params,
            "done": i + 1,
            "language": language,
            "segments": segments,
        })
    return segments, language


def _write_transcript_txt(path, segments: list[dict]) -> None:
    """人間が確認しやすいテキスト形式を出力する"""
    lines = []
    for seg in segments:
        lines.append(
            f"{format_timestamp(seg['start'])} --> {format_timestamp(seg['end'])}"
        )
        lines.append(seg["text"])
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")

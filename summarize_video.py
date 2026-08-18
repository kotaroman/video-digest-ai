#!/usr/bin/env python3
# 長時間動画から AI でダイジェスト動画を自動生成する CLI
import argparse
import sys
from pathlib import Path

from video_summary import blocks as blocks_mod
from video_summary import environment, scoring, selector, transcription, video
from video_summary.config import DEFAULT_OLLAMA_URL, Config
from video_summary.utils import (
    StageMeta,
    VideoSummaryError,
    format_hms,
    log,
    read_json,
    warn,
)


def _try_load(path, check):
    """再利用対象の JSON を読み込む。壊れていれば None を返して再計算させる"""
    try:
        data = read_json(path)
    except (ValueError, OSError) as e:
        warn(f"{path.name} を読み込めないため再計算します: {e}")
        return None
    if not check(data):
        warn(f"{path.name} の形式が想定と異なるため再計算します")
        return None
    return data


def parse_args(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        description="長時間動画を AI で要約し、ダイジェスト動画を生成します",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="入力動画ファイル (MP4 等)")
    parser.add_argument("--minutes", type=float, default=10.0,
                        help="ダイジェストの目標時間 (分)")
    parser.add_argument("--whisper-model", default="large-v3",
                        help="faster-whisper のモデル (large-v3 / turbo / medium / small 等)")
    parser.add_argument("--llm-model", default="qwen3:8b",
                        help="採点に使う Ollama モデル")
    parser.add_argument("--language", default="ja",
                        help="音声の言語コード")
    parser.add_argument("--block-seconds", type=float, default=40.0,
                        help="ブロックの目安時間 (秒)")
    parser.add_argument("--padding-before", type=float, default=1.0,
                        help="切り出し区間の前側余白 (秒)")
    parser.add_argument("--padding-after", type=float, default=1.0,
                        help="切り出し区間の後側余白 (秒)")
    parser.add_argument("--output-dir", type=Path, default=Path("output"),
                        help="中間ファイルと出力の保存先")
    parser.add_argument("--keep-clips", action="store_true",
                        help="結合後も個別クリップを残す")
    parser.add_argument("--force-cpu", action="store_true",
                        help="GPU を使わず CPU で文字起こしする")
    parser.add_argument("--resume", action="store_true",
                        help="既存の中間ファイルを再利用して途中から再開する")
    parser.add_argument("--goal", default="",
                        help="重要度判断の目的 (例: '技術解説と設定方法を優先し、雑談を除外する')")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL,
                        help="Ollama サーバーの URL")
    parser.add_argument("--score-batch-size", type=int, default=6,
                        help="1 回の LLM 呼び出しで採点するブロック数")
    args = parser.parse_args(argv)

    if args.minutes <= 0:
        parser.error("--minutes は正の値を指定してください")
    if args.block_seconds < 10:
        parser.error("--block-seconds は 10 以上を指定してください")
    if args.padding_before < 0 or args.padding_after < 0:
        parser.error("パディングは 0 以上を指定してください")

    return Config(
        input_path=args.input,
        minutes=args.minutes,
        whisper_model=args.whisper_model,
        llm_model=args.llm_model,
        language=args.language,
        block_seconds=args.block_seconds,
        padding_before=args.padding_before,
        padding_after=args.padding_after,
        output_dir=args.output_dir,
        keep_clips=args.keep_clips,
        force_cpu=args.force_cpu,
        resume=args.resume,
        goal=args.goal,
        ollama_url=args.ollama_url,
        score_batch_size=args.score_batch_size,
    )


def run_pipeline(config: Config) -> Path:
    if not config.input_path.is_file():
        raise VideoSummaryError(f"入力ファイルが存在しません: {config.input_path}")
    meta = StageMeta(config.meta_json, config.input_path)

    # 各ステージの生成条件 (これが変わったステージ以降は再計算される)
    transcript_params = {
        "whisper_model": config.whisper_model,
        "language": config.language,
    }
    blocks_params = {
        **transcript_params,
        "block_seconds": config.block_seconds,
    }
    # 採点の再利用判定には、これに加えて実際に生成されたブロック列の
    # fingerprint も使う (ブロック構築後でないと確定しないため後で合成する)
    scoring_base = {
        **blocks_params,
        "llm_model": config.llm_model,
        "goal": config.goal,
    }

    def scored_reusable_early() -> bool:
        """環境チェック時点で採点結果を再利用できる見込みか判定する。

        fingerprint はまだ確定しないため、上流 2 ステージが再利用可能で、
        かつ保存済みの採点パラメータが fingerprint を除いて一致する場合のみ
        Ollama のチェックを省略する。
        """
        if not (meta.can_reuse("transcript", transcript_params,
                               config.transcript_json)
                and meta.can_reuse("blocks", blocks_params, config.blocks_json)
                and config.scored_blocks_json.exists()):
            return False
        stored = meta.get("scored")
        if stored is None:
            return False
        return {k: v for k, v in stored.items() if k != "fingerprint"} \
            == scoring_base

    log("[1/6] Checking environment...")
    need_ollama = not (config.resume and scored_reusable_early())
    environment.check_environment(config, need_ollama)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    # --- 文字起こし ---
    log("")
    log("[2/6] Transcribing video...")
    transcript = None
    if config.resume and meta.can_reuse(
            "transcript", transcript_params, config.transcript_json):
        transcript = _try_load(
            config.transcript_json,
            lambda d: isinstance(d, dict) and isinstance(d.get("segments"), list),
        )
        if transcript is not None:
            log("  既存の transcript.json を再利用します")
    if transcript is None:
        transcript = transcription.transcribe(config)
        meta.mark_done("transcript", transcript_params)
    segments = transcript["segments"]
    log(f"Whisper segments: {len(segments)}")

    # --- ブロック化 ---
    log("")
    log("[3/6] Building blocks...")
    block_list = None
    if config.resume and meta.can_reuse(
            "blocks", blocks_params, config.blocks_json):
        block_list = _try_load(config.blocks_json,
                               lambda d: isinstance(d, list) and d)
        if block_list is not None:
            log("  既存の blocks.json を再利用します")
    if block_list is None:
        block_list = blocks_mod.build_and_save_blocks(config, segments)
        meta.mark_done("blocks", blocks_params)
    log(f"Blocks: {len(block_list)}")

    # --- 採点 ---
    log("")
    log(f"[4/6] Scoring blocks with Ollama ({config.llm_model})...")
    scored_params = {
        **scoring_base,
        "fingerprint": scoring.blocks_fingerprint(block_list),
    }
    scored = None
    if config.resume and meta.can_reuse(
            "scored", scored_params, config.scored_blocks_json):
        scored = _try_load(config.scored_blocks_json,
                           lambda d: isinstance(d, list) and d)
        if scored is not None:
            log("  既存の scored_blocks.json を再利用します")
    if scored is None:
        scored = scoring.score_blocks(config, block_list)
        meta.mark_done("scored", scored_params)

    # --- 重要区間の選択 ---
    log("")
    log("[5/6] Selecting highlights...")
    # 選択は軽量な処理のため毎回実行する (パラメータ変更を即座に反映できる)
    _, intervals = selector.select_blocks(config, block_list, scored)

    # --- 切り出しと結合 ---
    log("")
    log("[6/6] Rendering summary...")
    output_path = video.render_summary(config, intervals)
    return output_path


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    try:
        output_path = run_pipeline(config)
    except VideoSummaryError as e:
        print(f"\nエラー: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n中断しました。--resume を付けて再実行すると途中から再開できます。",
              file=sys.stderr)
        return 130

    log("")
    try:
        info = video.probe_video(output_path)
        log(f"Done: {output_path} ({format_hms(info['duration'])})")
    except VideoSummaryError:
        # 表示用の長さ取得に失敗しても、生成自体は完了している
        log(f"Done: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

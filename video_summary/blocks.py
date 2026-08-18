# Whisper セグメントを 30〜60 秒程度のブロックへ結合する
from .utils import write_json_atomic

# 文末とみなす末尾文字 (日本語と英語の両方に対応)
SENTENCE_ENDINGS = ("。", "!", "?", "!", "?", ".", "」", ")", ")")

# この秒数以上の無音ギャップは話題の切れ目とみなす
GAP_THRESHOLD = 2.0

# この秒数以上の無音ギャップはブロック長にかかわらず必ず境界にする。
# これがないと、短いブロックの直後に長い休憩・無音が来たとき
# ブロックが無音を丸ごと飲み込んでしまう。
LONG_GAP_THRESHOLD = 5.0


def _ends_sentence(text: str) -> bool:
    return text.endswith(SENTENCE_ENDINGS)


def build_blocks(segments: list[dict], block_seconds: float,
                 language: str = "ja") -> list[dict]:
    """セグメント列をブロック列へ変換する。

    目安時間 (デフォルト 40 秒) の前後で、以下の優先順で区切る:
      1. 長い無音ギャップ (>= 5 秒) は無条件で境界にする
      2. 無音ギャップ (>= 2 秒) かつ最低時間 (目安の 50%) を超えている
      3. 次のセグメントを足すと上限 (目安の 150%) を超える
      4. 文末で終わっていて目安の 75% を超えている
    """
    min_len = block_seconds * 0.5
    soft_len = block_seconds * 0.75
    max_len = block_seconds * 1.5
    joiner = "" if language == "ja" else " "

    blocks: list[dict] = []
    current: list[dict] = []

    def close_current() -> None:
        if not current:
            return
        start = current[0]["start"]
        end = current[-1]["end"]
        blocks.append({
            "id": len(blocks) + 1,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "text": joiner.join(s["text"] for s in current),
        })
        current.clear()

    for seg in segments:
        if current:
            cur_start = current[0]["start"]
            cur_end = current[-1]["end"]
            cur_dur = cur_end - cur_start
            gap = seg["start"] - cur_end
            new_dur = seg["end"] - cur_start

            if gap >= LONG_GAP_THRESHOLD:
                close_current()
            elif gap >= GAP_THRESHOLD and cur_dur >= min_len:
                close_current()
            elif new_dur > max_len:
                close_current()
            elif cur_dur >= soft_len and _ends_sentence(current[-1]["text"]):
                close_current()
        current.append(seg)
    close_current()

    return blocks


def build_and_save_blocks(config, segments: list[dict]) -> list[dict]:
    result = build_blocks(segments, config.block_seconds, config.language)
    write_json_atomic(config.blocks_json, result)
    return result

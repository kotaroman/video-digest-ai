# 目標時間に合わせた重要区間の選択 (ナップサック DP + 補正)
from .utils import log, warn, write_json_atomic

# 重複判定に使う文字 3-gram Jaccard 類似度のしきい値と減衰率
DUP_SIMILARITY_THRESHOLD = 0.55
DUP_PENALTY_FACTOR = 0.35

# 低スコアブロックの足切り
MIN_SCORE = 25

# 目標時間に対する許容誤差 (秒)
TOLERANCE = 5.0


def _trigrams(text: str) -> set[str]:
    normalized = "".join(text.split())
    return {normalized[i:i + 3] for i in range(len(normalized) - 2)}


def _apply_duplicate_penalty(blocks: list[dict]) -> None:
    """類似した内容のブロックのうちスコアの低い方を減衰させる。

    スコア降順に処理し、既に採用候補となった上位ブロックと類似していれば
    effective_score を下げる。同じ話の繰り返しを大量に選ばないための処理。
    """
    ordered = sorted(blocks, key=lambda b: b["score"], reverse=True)
    kept_grams: list[set[str]] = []
    for b in ordered:
        grams = _trigrams(b["text"])
        b["effective_score"] = float(b["score"])
        if grams:
            for other in kept_grams:
                union = len(grams | other)
                if union and len(grams & other) / union >= DUP_SIMILARITY_THRESHOLD:
                    b["effective_score"] = b["score"] * DUP_PENALTY_FACTOR
                    break
        kept_grams.append(grams)


def _knapsack(candidates: list[dict], capacity: int,
              pad_total: float) -> list[dict]:
    """0/1 ナップサック DP。重みはパディング込みの秒数 (整数)。

    価値は (effective_score - 20) * duration とし、スコアが高いほど
    秒あたりの価値が大きくなるようにする (低スコアの長尺で埋めない)。
    """
    n = len(candidates)
    weights = [max(1, round(b["duration"] + pad_total)) for b in candidates]
    values = [max(b["effective_score"] - 20.0, 1.0) * b["duration"]
              for b in candidates]

    dp = [0.0] * (capacity + 1)
    keep = [bytearray(capacity + 1) for _ in range(n)]
    for i in range(n):
        w, v = weights[i], values[i]
        row = keep[i]
        for c in range(capacity, w - 1, -1):
            cand = dp[c - w] + v
            if cand > dp[c]:
                dp[c] = cand
                row[c] = 1

    chosen = []
    c = capacity
    for i in range(n - 1, -1, -1):
        if keep[i][c]:
            chosen.append(candidates[i])
            c -= weights[i]
    return chosen


def merge_padded_intervals(blocks: list[dict], padding_before: float,
                           padding_after: float,
                           video_duration: float) -> list[dict]:
    """各ブロックにパディングを付与し、重複・近接する区間をマージする"""
    raw = []
    for b in sorted(blocks, key=lambda x: x["start"]):
        start = max(0.0, b["start"] - padding_before)
        end = b["end"] + padding_after
        if video_duration > 0:
            # プローブ誤差でタイムスタンプが動画長を超えることがあるため両端を丸める
            start = min(start, video_duration)
            end = min(end, video_duration)
        if end - start < 0.2:
            continue  # 潰れた区間は FFmpeg に渡さない
        raw.append((start, end))

    merged: list[list[float]] = []
    for start, end in raw:
        # 0.5 秒未満の隙間は 2 フレーム程度の細切れクリップになるためマージする
        if merged and start <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [
        {"start": round(s, 3), "end": round(e, 3), "duration": round(e - s, 3)}
        for s, e in merged
    ]


def select_blocks(config, blocks: list[dict],
                  scored: list[dict]) -> tuple[list[dict], list[dict]]:
    """重要区間を選択し selected_blocks.json を保存する。

    返り値: (選択ブロック一覧, パディング適用済みの切り出し区間一覧)
    """
    score_by_id = {s["id"]: s for s in scored}
    merged_blocks = []
    for b in blocks:
        s = score_by_id.get(b["id"])
        if s is None:
            continue
        merged_blocks.append({**b, "score": s["score"], "reason": s["reason"]})

    _apply_duplicate_penalty(merged_blocks)

    target = config.target_seconds
    pad_total = config.padding_before + config.padding_after

    def merged_total(selection: list[dict]) -> float:
        intervals = merge_padded_intervals(
            selection, config.padding_before, config.padding_after,
            config.video_duration,
        )
        return sum(iv["duration"] for iv in intervals)

    # 動画全体が目標以下なら全ブロックを選択
    total_available = sum(b["duration"] for b in merged_blocks)
    if total_available <= target:
        warn("採点対象の総時間が目標時間以下のため、全ブロックを選択します")
        selection = list(merged_blocks)
        # パディング分で目標を超える場合は低スコアのブロックから外す
        while len(selection) > 1 and merged_total(selection) > target + TOLERANCE:
            worst = min(selection, key=lambda b: b["effective_score"])
            selection.remove(worst)
    else:
        candidates = [b for b in merged_blocks
                      if b["effective_score"] >= MIN_SCORE]
        if sum(b["duration"] for b in candidates) < target:
            candidates = list(merged_blocks)  # 足切りすると足りない場合は全件対象

        selection = _knapsack(candidates, round(target), pad_total)
        if not selection and candidates:
            # 目標時間が短すぎてどのブロックも入らない場合は最高スコアの 1 件
            best = max(candidates, key=lambda b: b["effective_score"])
            warn(
                f"目標時間 ({target:.0f} 秒) がブロック長より短いため、"
                f"最高スコアのブロック 1 件 ({best['duration']:.0f} 秒) を選択します"
            )
            selection = [best]

        selected_ids = {b["id"] for b in selection}
        remaining = [b for b in candidates if b["id"] not in selected_ids]

        # 連続性の補正: 選択済みブロックに挟まれた孤立ブロックを優先的に追加
        sandwiched = [b for b in remaining
                      if b["id"] - 1 in selected_ids and b["id"] + 1 in selected_ids
                      and b["effective_score"] >= 35]
        for b in sorted(sandwiched, key=lambda x: x["effective_score"],
                        reverse=True):
            trial = selection + [b]
            if merged_total(trial) <= target + TOLERANCE:
                selection = trial
                selected_ids.add(b["id"])

        # マージ後の実時間ベースで目標時間まで詰める
        remaining = [b for b in remaining if b["id"] not in selected_ids]
        remaining.sort(key=lambda b: b["effective_score"], reverse=True)
        current_total = merged_total(selection)
        for b in remaining:
            if current_total >= target - TOLERANCE:
                break
            trial = selection + [b]
            trial_total = merged_total(trial)
            if trial_total <= target + TOLERANCE:
                selection = trial
                current_total = trial_total

    selection.sort(key=lambda b: b["start"])
    intervals = merge_padded_intervals(
        selection, config.padding_before, config.padding_after,
        config.video_duration,
    )
    total = sum(iv["duration"] for iv in intervals)

    write_json_atomic(config.selected_blocks_json, {
        "target_seconds": target,
        "total_duration": round(total, 3),
        "block_count": len(selection),
        "blocks": [
            {
                "id": b["id"], "start": b["start"], "end": b["end"],
                "duration": b["duration"], "score": b["score"],
                "effective_score": round(b["effective_score"], 1),
                "reason": b["reason"], "text": b["text"],
            }
            for b in selection
        ],
        "intervals": intervals,
    })
    log(f"  Selected: {len(selection)} blocks")
    log(f"  Duration: {total:.1f} sec (目標 {target:.0f} 秒)")
    return selection, intervals

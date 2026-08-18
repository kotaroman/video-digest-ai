# Ollama によるブロック重要度の採点
#
# 安定性のための工夫:
#   - Ollama の Structured Output (format に JSON Schema) で出力形式を固定
#   - バッチ採点に失敗したブロックは単独採点へフォールバック
#   - それでも失敗した場合はデフォルト点 (50) を与えて処理を止めない
#   - バッチごとに部分結果を保存し、中断しても --resume で続きから再開できる
import hashlib
import json
import re
import time

import requests

from .utils import VideoSummaryError, log, warn, read_json, write_json_atomic

DEFAULT_SCORE = 50
REQUEST_TIMEOUT = (10, 900)  # (接続, 応答) 秒。初回はモデルロードで時間がかかる

BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "reason": {"type": "string"},
                },
                "required": ["id", "score", "reason"],
            },
        }
    },
    "required": ["scores"],
}

SINGLE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
}

THEME_SCHEMA = {
    "type": "object",
    "properties": {"theme": {"type": "string"}},
    "required": ["theme"],
}

RUBRIC = """採点基準 (0〜100):
- 90〜100: 絶対に残す価値が高い。結論、重要な説明、問題発生、解決方法、結果、重要な実演など
- 70〜89: かなり重要
- 40〜69: 補足情報として有用
- 0〜39: 挨拶、雑談、重複、間延びした説明、脱線、内容の薄い部分"""


def _mmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60:02d}:{total % 60:02d}"


class ScoringError(Exception):
    """採点 1 回分の失敗 (フォールバックで処理を続ける)"""


class OllamaScorer:
    def __init__(self, config):
        self.url = config.ollama_url
        self.model = config.llm_model
        self.goal = config.goal
        self.session = requests.Session()
        # think パラメータ対応はモデルによって異なるため初回呼び出しで判定する
        self._supports_think: bool | None = None

    # --- 低レベル API ---

    def _post(self, payload: dict) -> requests.Response:
        """POST /api/chat。一時的な切断は 2 秒待って 1 回だけ再試行する"""
        try:
            return self.session.post(
                f"{self.url}/api/chat", json=payload, timeout=REQUEST_TIMEOUT
            )
        except requests.Timeout:
            raise
        except requests.RequestException:
            time.sleep(2)
            return self.session.post(
                f"{self.url}/api/chat", json=payload, timeout=REQUEST_TIMEOUT
            )

    def _chat(self, system: str, user: str, schema: dict,
              num_predict: int) -> dict:
        """Ollama /api/chat を呼び、JSON をパースして返す"""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": schema,
            "options": {
                "temperature": 0.2,
                "seed": 42,
                "num_ctx": 16384,
                "num_predict": num_predict,
            },
        }
        # thinking モデル (qwen3 等) は思考を無効化して速度と安定性を優先する
        if self._supports_think is not False:
            payload["think"] = False

        try:
            res = self._post(payload)
        except requests.Timeout as e:
            # タイムアウトは一時的な可能性が高いので、呼び出し側の
            # 再試行・単独採点フォールバックに任せる
            raise ScoringError(f"Ollama への要求がタイムアウトしました: {e}")
        except requests.RequestException as e:
            raise VideoSummaryError(
                f"Ollama への接続に失敗しました。サーバーが停止した可能性があります。\n"
                f"  採点の途中結果は保存済みのため、サーバー復旧後に --resume で再開できます。\n"
                f"  詳細: {e}"
            )

        if res.status_code == 400 and self._supports_think is None:
            # think 非対応モデルの場合は外して 1 回だけやり直す
            body = res.text.lower()
            if "think" in body:
                self._supports_think = False
                return self._chat(system, user, schema, num_predict)
        if self._supports_think is None and res.status_code == 200:
            self._supports_think = True

        if res.status_code == 404:
            raise VideoSummaryError(
                f"Ollama モデル '{self.model}' が見つかりません。"
                f"`ollama pull {self.model}` を実行してください。"
            )
        if res.status_code != 200:
            raise ScoringError(f"Ollama API エラー (HTTP {res.status_code}): {res.text[:300]}")

        content = res.json().get("message", {}).get("content", "")
        return self._extract_json(content)

    @staticmethod
    def _extract_json(text: str) -> dict:
        """LLM 出力から JSON を取り出す。<think> ブロックや前後の混入に耐える"""
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ScoringError(f"LLM 出力に JSON が含まれていません: {text[:200]}")
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as e:
            raise ScoringError(f"LLM 出力の JSON パースに失敗しました: {e}")

    # --- テーマ要約 ---

    def summarize_theme(self, blocks: list[dict]) -> str:
        """動画全体のテーマを 2〜3 文で要約する (採点時の文脈として使う)"""
        samples = self._sample_blocks(blocks, max_samples=14, max_chars=100)
        body = "\n".join(f"[{_mmss(b['start'])}] {t}" for b, t in samples)
        system = (
            "あなたは動画編集アシスタントです。"
            "文字起こしの抜粋から動画全体のテーマを日本語 2〜3 文で要約してください。"
        )
        user = f"以下は動画の文字起こしの抜粋です。\n\n{body}\n\nこの動画全体のテーマを要約してください。"
        try:
            result = self._chat(system, user, THEME_SCHEMA, num_predict=512)
            theme = str(result.get("theme", "")).strip()
            if theme:
                log(f"  動画テーマ: {theme}")
            return theme
        except ScoringError as e:
            warn(f"テーマ要約に失敗しました (採点は続行します): {e}")
            return ""

    @staticmethod
    def _sample_blocks(blocks: list[dict], max_samples: int,
                       max_chars: int) -> list[tuple[dict, str]]:
        if len(blocks) <= max_samples:
            chosen = blocks
        else:
            step = (len(blocks) - 1) / (max_samples - 1)
            indices = sorted({round(i * step) for i in range(max_samples)})
            chosen = [blocks[i] for i in indices]
        return [(b, b["text"][:max_chars]) for b in chosen]

    # --- 採点 ---

    def _system_prompt(self) -> str:
        parts = [
            "あなたは動画編集アシスタントです。"
            "長時間動画からダイジェストを作るため、文字起こしブロックの重要度を採点します。",
            RUBRIC,
        ]
        if self.goal:
            parts.append(f"この動画の要約の目的: {self.goal}\nこの目的に合致する内容を高く採点してください。")
        parts.append(
            "動画全体のテーマと前後の文脈を踏まえて判断してください。"
            "各ブロックについて id・score・reason (日本語で簡潔に) を返してください。"
        )
        return "\n\n".join(parts)

    def score_batch(self, batch: list[dict], theme: str,
                    prev_text: str) -> dict[int, dict]:
        """複数ブロックをまとめて採点する。返り値は id -> {score, reason}"""
        lines = []
        if theme:
            lines.append(f"動画全体のテーマ: {theme}\n")
        if prev_text:
            lines.append(f"直前のブロックの内容 (文脈参照用、採点対象外):\n{prev_text[:300]}\n")
        lines.append(f"以下の {len(batch)} 個のブロックをすべて採点してください。\n")
        for b in batch:
            lines.append(f"[id={b['id']}] {_mmss(b['start'])} - {_mmss(b['end'])}")
            lines.append(b["text"])
            lines.append("")
        result = self._chat(
            self._system_prompt(), "\n".join(lines),
            BATCH_SCHEMA, num_predict=4096,
        )

        scored: dict[int, dict] = {}
        wanted = {b["id"] for b in batch}
        for item in result.get("scores", []):
            try:
                block_id = int(item["id"])
                score = max(0, min(100, int(item["score"])))
                reason = str(item.get("reason", "")).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if block_id in wanted:
                scored[block_id] = {"score": score, "reason": reason}
        return scored

    def score_single(self, block: dict, theme: str) -> dict:
        lines = []
        if theme:
            lines.append(f"動画全体のテーマ: {theme}\n")
        lines.append(
            f"次のブロックを採点してください。\n"
            f"[id={block['id']}] {_mmss(block['start'])} - {_mmss(block['end'])}\n"
            f"{block['text']}"
        )
        result = self._chat(
            self._system_prompt(), "\n".join(lines),
            SINGLE_SCHEMA, num_predict=512,
        )
        return {
            "score": max(0, min(100, int(result["score"]))),
            "reason": str(result.get("reason", "")).strip(),
        }


def blocks_fingerprint(blocks: list[dict]) -> str:
    key = json.dumps([(b["id"], b["start"], b["end"]) for b in blocks])
    return hashlib.md5(key.encode()).hexdigest()


def score_blocks(config, blocks: list[dict]) -> list[dict]:
    """全ブロックを採点し scored_blocks.json を保存する"""
    scorer = OllamaScorer(config)

    partial_params = {
        "llm_model": config.llm_model,
        "goal": config.goal,
        "fingerprint": blocks_fingerprint(blocks),
    }

    # 部分結果の読み込み (--resume 時のみ)
    scores: dict[int, dict] = {}
    theme = ""
    if config.resume and config.scored_partial_json.exists():
        try:
            partial = read_json(config.scored_partial_json)
            if partial.get("params") == partial_params:
                scores = {int(k): v for k, v in partial.get("scores", {}).items()}
                theme = partial.get("theme", "")
                log(f"  部分採点結果を再利用: {len(scores)}/{len(blocks)} ブロック")
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    if not theme:
        theme = scorer.summarize_theme(blocks)

    def save_partial() -> None:
        write_json_atomic(config.scored_partial_json, {
            "params": partial_params,
            "theme": theme,
            "scores": {str(k): v for k, v in scores.items()},
        })

    remaining = [b for b in blocks if b["id"] not in scores]
    block_by_id = {b["id"]: b for b in blocks}
    batch_size = max(1, config.score_batch_size)

    for i in range(0, len(remaining), batch_size):
        batch = remaining[i:i + batch_size]

        # 直前ブロックの末尾を文脈として渡す
        first_id = batch[0]["id"]
        prev = block_by_id.get(first_id - 1)
        prev_text = prev["text"][-300:] if prev else ""

        batch_scores: dict[int, dict] = {}
        for attempt in range(2):
            try:
                batch_scores = scorer.score_batch(batch, theme, prev_text)
                break
            except ScoringError as e:
                if attempt == 0:
                    warn(f"バッチ採点に失敗、再試行します: {e}")
                else:
                    warn(f"バッチ採点の再試行も失敗、単独採点に切り替えます: {e}")

        # バッチで取れなかったブロックは単独採点 → それも駄目ならデフォルト点
        for b in batch:
            if b["id"] in batch_scores:
                scores[b["id"]] = batch_scores[b["id"]]
                continue
            try:
                scores[b["id"]] = scorer.score_single(b, theme)
            except (ScoringError, KeyError, TypeError, ValueError) as e:
                warn(f"ブロック {b['id']} の採点に失敗、デフォルト点を使用します: {e}")
                scores[b["id"]] = {
                    "score": DEFAULT_SCORE,
                    "reason": "採点に失敗したためデフォルト値",
                }
        save_partial()
        log(f"  Scoring {len(scores)}/{len(blocks)}")

    result = [
        {"id": b["id"], "score": scores[b["id"]]["score"],
         "reason": scores[b["id"]]["reason"]}
        for b in blocks
    ]
    write_json_atomic(config.scored_blocks_json, result)
    config.scored_partial_json.unlink(missing_ok=True)
    return result

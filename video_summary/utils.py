# 共通ユーティリティ (ログ・JSON 入出力・時刻フォーマット・再開管理)
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


class VideoSummaryError(Exception):
    """ユーザーに原因を提示して終了するべきエラー"""


def log(message: str = "") -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"警告: {message}", file=sys.stderr, flush=True)


def format_timestamp(seconds: float) -> str:
    """秒数を 00:00:10.520 形式に変換する"""
    if seconds < 0:
        seconds = 0.0
    total_ms = round(seconds * 1000)
    ms = total_ms % 1000
    total_sec = total_ms // 1000
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def format_hms(seconds: float) -> str:
    """秒数を 1:23:45 のような表示用文字列に変換する"""
    total = int(round(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: Path, data: Any) -> None:
    """中断時に壊れた JSON が残らないよう、一時ファイル経由で書き込む"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


class StageMeta:
    """中間ファイルの生成条件を記録し、--resume 時に再利用可否を判定する。

    output/meta.json に「入力動画の識別情報」と「各ステージのパラメータ」を
    保存する。パラメータが変わっていた場合はそのステージ以降を再計算する。
    """

    # パイプラインの依存順。上流を再計算したら下流の記録を無効化する
    STAGE_ORDER = ["transcript", "blocks", "scored"]

    def __init__(self, meta_path: Path, input_path: Path):
        self.meta_path = meta_path
        self.input_key = self._input_key(input_path)
        self.data: dict = {"input": self.input_key, "stages": {}}
        if meta_path.exists():
            try:
                loaded = read_json(meta_path)
                if (isinstance(loaded, dict)
                        and isinstance(loaded.get("stages"), dict)
                        and loaded.get("input") == self.input_key):
                    self.data = loaded
            except (ValueError, OSError):
                pass  # 壊れた meta は無視して作り直す

    @staticmethod
    def _input_key(input_path: Path) -> dict:
        stat = input_path.stat()
        return {
            "name": input_path.name,
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
        }

    def get(self, stage: str) -> dict | None:
        params = self.data.get("stages", {}).get(stage)
        return params if isinstance(params, dict) else None

    def can_reuse(self, stage: str, params: dict, output_file: Path) -> bool:
        """出力ファイルが存在し、かつ生成時パラメータが一致していれば再利用可"""
        if not output_file.exists():
            return False
        return self.get(stage) == params

    def mark_done(self, stage: str, params: dict) -> None:
        """ステージ完了を記録する。下流ステージの記録は古くなるため破棄する"""
        stages = self.data.setdefault("stages", {})
        stages[stage] = params
        if stage in self.STAGE_ORDER:
            for later in self.STAGE_ORDER[self.STAGE_ORDER.index(stage) + 1:]:
                stages.pop(later, None)
        write_json_atomic(self.meta_path, self.data)

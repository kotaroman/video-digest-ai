# CLI 引数から生成される実行設定
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OLLAMA_URL = "http://localhost:11434"


@dataclass
class Config:
    input_path: Path
    minutes: float = 10.0
    whisper_model: str = "large-v3"
    llm_model: str = "qwen3:8b"
    language: str = "ja"
    block_seconds: float = 40.0
    padding_before: float = 1.0
    padding_after: float = 1.0
    output_dir: Path = Path("output")
    keep_clips: bool = False
    force_cpu: bool = False
    resume: bool = False
    goal: str = ""
    ollama_url: str = DEFAULT_OLLAMA_URL
    score_batch_size: int = 6

    # 環境チェックで決まる実行時の値
    use_nvenc: bool = False
    use_cuda: bool = False
    video_duration: float = 0.0

    @property
    def target_seconds(self) -> float:
        return self.minutes * 60.0

    @property
    def summary_path(self) -> Path:
        m = self.minutes
        label = str(int(m)) if float(m).is_integer() else f"{m:g}"
        return self.output_dir / f"summary_{label}min.mp4"

    # --- 中間ファイルのパス ---
    @property
    def meta_json(self) -> Path:
        return self.output_dir / "meta.json"

    @property
    def transcript_json(self) -> Path:
        return self.output_dir / "transcript.json"

    @property
    def transcript_partial_json(self) -> Path:
        return self.output_dir / "transcript.partial.json"

    @property
    def transcript_txt(self) -> Path:
        return self.output_dir / "transcript.txt"

    @property
    def blocks_json(self) -> Path:
        return self.output_dir / "blocks.json"

    @property
    def scored_blocks_json(self) -> Path:
        return self.output_dir / "scored_blocks.json"

    @property
    def scored_partial_json(self) -> Path:
        return self.output_dir / "scored_blocks.partial.json"

    @property
    def selected_blocks_json(self) -> Path:
        return self.output_dir / "selected_blocks.json"

    @property
    def clips_dir(self) -> Path:
        return self.output_dir / "clips"

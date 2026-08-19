# CLAUDE.md

長時間動画(2時間級)からローカル AI のみでダイジェスト動画を生成する Python CLI。
パイプライン: faster-whisper(GPU)→ Ollama 採点 → 区間選択 → FFmpeg(NVENC)切り出し・結合。
発話のない動画向けに `--mode uniform` / `--mode timelapse` がある。

## コマンド

- 実行: `./summarize input.mp4 --minutes 10`(venv を自動使用。activate 不要)
- セットアップ: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
- 構文チェック: `.venv/bin/python -m py_compile summarize_video.py video_summary/*.py`
- 自動テストスイートはない。動作確認は「検証」の手順で行う

## 絶対に守る制約

- ローカル AI のみ。有料 API(OpenAI / Anthropic / Gemini 等)の追加や、動画・音声・文字起こし内容の外部送信をしない
- WSL2 前提。WSL 内に Linux 版 NVIDIA ドライバや CUDA Toolkit を入れない(Windows 側ドライバのパススルーと競合する)。cuBLAS / cuDNN は pip の nvidia-* パッケージを起動時に preload して解決している(environment.py)
- `input/` と `output/` はユーザーの実データ。削除・上書き・テストへの流用をしない
- `tmp/` はユーザーの IDE 用作業ディレクトリ。触らない

## 検証

- 実動画(2時間級)で動作確認しない(処理に数十分かかる)。ffmpeg の lavfi で短いフィクスチャを生成して使う:
  `ffmpeg -f lavfi -i testsrc2=duration=60:rate=30 -f lavfi -i sine=frequency=440:duration=60 -c:v libx264 -c:a aac -shortest fixture.mp4`
- `--output-dir` は必ず一時ディレクトリへ向ける
- 出力の確認: ffprobe で duration とストリーム構成、`ffmpeg -v error -i 出力.mp4 -f null -` でデコードエラーがないこと
- クリップ結合は concat demuxer の `-c copy` で行うため、全クリップのストリーム構成一致が前提。音声処理を変更したら、音声なし / 途中から音声が始まる / チャンネル構成が異なる入力でも確認する

## スタイル・規約

- コメント・ログ・エラーメッセージは日本語。識別子・ブランチ名は英語
- 利用者向けエラーは VideoSummaryError に日本語メッセージと対処ヒントを添えて raise する
- コミットメッセージ: `<type>: <日本語説明>`(type: feat / fix / chore / refactor / docs / test / ci)
- コミット・プッシュはユーザーの承認を得てから行う

## 開発ハーネス

計画→実装→検証を分離した 3 エージェント(planner / generator / evaluator、`.claude/agents/`)がある。
`/harness <依頼内容>` で起動する(手順は `.claude/skills/harness/SKILL.md`)。

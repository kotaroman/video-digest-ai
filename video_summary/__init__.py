# 動画自動要約ツール
#
# モジュール構成 (V1 は音声ベース):
#   transcription : faster-whisper による文字起こし
#   blocks        : セグメントのブロック化
#   scoring       : Ollama による重要度採点
#   selector      : 目標時間に合わせた重要区間の選択
#   video         : FFmpeg による切り出し・結合
#
# 将来的に映像フレーム解析 (Vision) を追加する場合は、
# scoring と並列に vision モジュールを追加し、selector で
# 両方のスコアを統合する構成を想定している。

__version__ = "0.1.0"

# video-digest-ai

長時間動画(2時間程度)をローカルAIで解析し、重要部分だけを抜き出した短いダイジェスト動画(デフォルト約10分)を自動生成するCLIツールです。

- 文字起こし: [faster-whisper](https://github.com/SYSTRAN/faster-whisper)(GPU)
- 重要度評価: [Ollama](https://ollama.com/) のローカルLLM(デフォルト `qwen3:8b`)
- 切り出し・結合: FFmpeg(NVENC対応)

外部APIは使用せず、動画・音声・文字起こし内容が外部へ送信されることはありません。

## 1. 必要環境

- Windows 11 + WSL2 (Ubuntu)
- NVIDIA GPU(Windows側ドライバのみでよい。WSL内にLinux版ドライバは入れないこと)
- Python 3.11 以降
- FFmpeg / ffprobe
- Ollama

## 2. GPU確認

WSL内で以下が動けばOK(Windows側ドライバがWSLへパススルーされています):

```bash
nvidia-smi
```

動かない場合はWindows側のNVIDIAドライバを更新し、PowerShellで `wsl --update` を実行してください。

## 3. FFmpeg確認

```bash
ffmpeg -version
ffprobe -version
```

未インストールなら:

```bash
sudo apt update && sudo apt install -y ffmpeg
```

## 4. Ollama確認

```bash
ollama --version
ollama list
```

## 5. モデル取得

```bash
ollama pull qwen3:8b
```

## 6. Python仮想環境とインストール

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

以降の実行は付属の `./summarize` ランチャーが venv の Python を自動で使うため、`source .venv/bin/activate` は不要です。

### WSL + NVIDIA GPU で faster-whisper を動かす際の注意

- **CUDA Toolkit のインストールは不要です。** faster-whisper (CTranslate2) の実行に必要な cuBLAS / cuDNN は、`requirements.txt` に含まれる pip パッケージ(`nvidia-cublas-cu12` / `nvidia-cudnn-cu12`)だけで揃います。本ツールはこれらのライブラリを起動時に自動ロードするため、`LD_LIBRARY_PATH` の設定も不要です。
- WSL内にLinux版NVIDIAドライバや `nvidia-cuda-toolkit` パッケージを入れないでください(WSLのGPUパススルーと競合します)。
- RTX 50系 (Blackwell) では `ctranslate2>=4.6.0` が必要です(requirements.txt で指定済み)。

## 7. 実行

```bash
./summarize input/video.mp4 --minutes 10
```

完了すると `output/summary_10min.mp4` が生成されます。

### 主なオプション

| オプション | デフォルト | 説明 |
|---|---|---|
| `--minutes` | 10 | ダイジェストの目標時間(分) |
| `--whisper-model` | large-v3 | VRAM不足時は `turbo` / `medium` / `small` |
| `--llm-model` | qwen3:8b | 採点に使うOllamaモデル |
| `--language` | ja | 音声の言語 |
| `--block-seconds` | 40 | 評価単位ブロックの目安秒数 |
| `--padding-before` / `--padding-after` | 1.0 | 切り出し区間の前後余白(秒) |
| `--goal` | なし | 重要度判断の目的(下記) |
| `--mode` | speech | `speech` / `uniform` / `timelapse`(下記) |
| `--clip-seconds` | 30 | uniform モードの1クリップの長さ(秒) |
| `--output-dir` | ./output | 出力先 |
| `--keep-clips` | off | 個別クリップ(output/clips/)を残す |
| `--force-cpu` | off | GPUを使わず文字起こし(低速) |
| `--resume` | off | 中間ファイルを再利用して途中から再開(speechモードのみ) |
| `--version` | - | バージョンを表示して終了 |

### goal で判断基準を変える

```bash
./summarize input/video.mp4 --minutes 10 \
  --goal "家庭菜園初心者に役立つ説明、実演、結果を優先する"
```

### 発話が少ない動画をダイジェストにする(--mode)

デフォルトの `speech` モードは発話を材料に要約するため、ナレーションや会話がほぼない動画(作業風景の録画など)では数秒のダイジェストしか作れません。その場合は映像ベースの2モードを使います:

```bash
# 等間隔サンプリング: 全体から30秒クリップを等間隔に切り出して目標時間にする(音声あり)
./summarize input/video.mp4 --minutes 10 --mode uniform

# タイムラプス: 全編を倍速圧縮して目標時間にする(音声なし)
./summarize input/video.mp4 --minutes 10 --mode timelapse
```

- どちらも Whisper / Ollama を使わないため、GPU の文字起こし環境や Ollama がなくても実行できます(音声トラックのない動画も可)。
- 出力は `summary_uniform_10min.mp4` / `summary_timelapse_10min.mp4` のようにモード名付きになり、speech モードの出力を上書きしません。
- timelapse は全編を再エンコードするため長時間動画では時間がかかります(進捗が10%刻みで表示されます)。

### 途中から再開する(speechモードのみ)

speechモードの処理は段階ごとに `output/` へ保存されます。中断した場合や、パラメータを一部だけ変えて再実行する場合は `--resume` を付けると、再計算が必要なステージだけが実行されます(例: `--goal` だけ変えた場合、文字起こしは再利用され採点からやり直し)。uniform / timelapse は中間ファイルを持たないため、中断した場合はそのまま再実行してください。

```text
output/
├── transcript.json      # 文字起こし(タイムスタンプ付き)
├── transcript.txt       # 確認用テキスト
├── blocks.json          # 約40秒単位のブロック
├── scored_blocks.json   # LLMによる重要度(0-100)と理由
├── selected_blocks.json # 選択された区間
├── clips/               # 切り出しクリップ(--keep-clips 時のみ残る)
└── summary_10min.mp4    # 完成ダイジェスト
```

採点は数ブロックごとに部分保存されるため、採点途中で中断しても `--resume` で続きから再開できます。15分を超える動画の文字起こしはメモリ節約のため無音位置で約10分のチャンクに分割して処理され、こちらもチャンク単位で部分保存・再開されます。

## Windows側の動画を処理する

Windowsのファイルは WSL から `/mnt/c/...` で読めます:

```bash
./summarize "/mnt/c/Users/xxx/Videos/video.mp4"
```

ただし `/mnt/c` 経由のI/Oは遅いため、長時間動画はWSL側ファイルシステムへコピーしてから処理する方が高速です:

```bash
mkdir -p ~/videos
cp /mnt/c/Users/xxx/Videos/video.mp4 ~/videos/
./summarize ~/videos/video.mp4
```

## 処理の流れ

1. 環境チェック(ffmpeg / GPU / CUDA / Ollama / NVENC実測)
2. faster-whisper で文字起こし(VADフィルタ有効。15分超の動画は無音位置で約10分のチャンクに分割し、省メモリで処理)
3. セグメントを文末・無音ギャップを考慮して約40秒のブロックへ結合
4. Ollama でブロックを一括採点(Structured Outputで JSON を安定取得、失敗時は単独採点へフォールバック)
5. ナップサックDP + 重複ペナルティ + 連続性補正で目標時間に近い区間集合を選択
6. FFmpeg で切り出し(NVENC、不可なら libx264)→ 無劣化結合

speech モードの解析は音声ベースのみです。発話のない動画には `--mode uniform` / `--mode timelapse` を使用してください。将来的に映像フレーム解析(Vision)を追加できるよう、採点モジュールは独立させてあります。

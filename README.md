# Simple H.264 Editor

AviUtl風のUXで、H.264動画のクロップ・トリム・解像度変換をGUIで行えるシンプルな編集ツールです。

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![PySide6](https://img.shields.io/badge/PySide6-6.6%2B-green)
![FFmpeg](https://img.shields.io/badge/FFmpeg-required-orange)

## 機能

| 機能 | 説明 |
|---|---|
| **動画読み込み** | ファイルダイアログ または ドラッグ&ドロップ |
| **プレビュー再生** | In/Out範囲内でループ再生しながら確認 |
| **トリム** | タイムライン上の緑(In)・赤(Out)ハンドルで切り出し範囲を指定 |
| **クロップ** | 動画上の四隅ハンドルをドラッグして切り抜き範囲を指定、数値入力にも対応 |
| **解像度選択** | 4K / 2K / 1080p / 720p / 480p / Original / Custom から選択 |
| **エクスポート** | FFmpeg (libx264 CRF23) でMP4に書き出し、進行状況表示あり |

## スクリーンショット

```
┌─────────────────────────────────────────────────────┐
│                                                     │
│         [動画プレビュー + クロップハンドル]              │
│         ┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐                     │
│         │   (クロップ範囲)    │                     │
│         └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘                     │
│                                                     │
├─────────────────────────────────────────────────────┤
│  [====|▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓|====]  ← タイムライン        │
│       In              Out                           │
├─────────────────────────────────────────────────────┤
│ [開く] [▶ 再生]  00:12.345 / 01:30.000  [In] [Out] │
├──────────────┬──────────────┬───────────────────────┤
│ クロップ      │ 出力解像度    │                       │
│ X:0   Y:0   │ 1080p        │  [エクスポート H.264]  │
│ W:1920 H:1080│ Custom: W H  │                       │
└──────────────┴──────────────┴───────────────────────┘
```

## インストール

### 前提条件

- Python 3.10 以上
- macOS または Windows 10 以降

### macOS

```bash
# 1. FFmpeg をインストール
brew install ffmpeg

# 2. リポジトリをクローン
git clone https://github.com/dskmraug/simple-h264-encoder.git
cd simple-h264-encoder

# 3. PySide6 をインストール
pip3 install -r requirements.txt

# 4. 起動
python3 editor.py
```

### Windows

```powershell
# 1. FFmpeg をインストール（winget を使う場合）
winget install ffmpeg

# または https://ffmpeg.org/download.html から手動でダウンロードし、
# 解凍した bin フォルダを PATH に追加してください。

# 2. リポジトリをクローン
git clone https://github.com/dskmraug/simple-h264-encoder.git
cd simple-h264-encoder

# 3. PySide6 をインストール
pip install -r requirements.txt

# 4. 起動
python editor.py
```

#### Windows での動画再生について

動画再生には Windows 標準の **Windows Media Foundation (WMF)** バックエンドが使われます。
H.264 / MP4 は Windows 10 以降で標準対応しており、通常は追加設定不要です。

`.mov` など WMF が対応していない形式を再生したい場合、または再生が不安定な場合は、
Qt の **FFmpeg マルチメディアバックエンド**に切り替えることで改善することがあります。

```powershell
# コマンドプロンプト / PowerShell で設定してから起動する場合
set QT_MEDIA_BACKEND=ffmpeg      # cmd.exe
$env:QT_MEDIA_BACKEND="ffmpeg"   # PowerShell
python editor.py
```

永続的に設定する場合は、システムの「環境変数の編集」から
`QT_MEDIA_BACKEND` = `ffmpeg` を追加してください。

> **注意:** FFmpeg バックエンドはソフトウェアデコードを使用するため、
> Mac の AVFoundation と比べて CPU 使用率がやや高くなる場合があります。

## キーボードショートカット

| キー | 動作 |
|---|---|
| `Space` | 再生 / 一時停止 |
| `I` | 現在位置を In 点にセット |
| `O` | 現在位置を Out 点にセット |
| `←` / `→` | 500ms 移動 |
| `Shift + ←` / `→` | 100ms 移動（フレーム単位の微調整） |

## 対応フォーマット

**入力:** MP4, MOV, M4V, MKV, AVI, H.264, TS など（macOS: AVFoundation、Windows: WMF が対応する形式）

**出力:** MP4 (H.264 / AAC)

## 依存ライブラリ

- [PySide6](https://pypi.org/project/PySide6/) — Qt6 Python バインディング（GUI・動画再生）
- [FFmpeg](https://ffmpeg.org/) — 動画エンコード・フィルタ処理

## ライセンス

MIT

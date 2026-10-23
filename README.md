<h1 align="center">SHAKALIZER</h1>

<p align="center">
  <img src="https://img.shields.io/badge/OPEN%20SOURCE-24292f?style=for-the-badge&logo=github&logoColor=white" alt="Open Source">
  <img src="https://img.shields.io/badge/PYTHON-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FREE-238636?style=for-the-badge&logo=checkmarx&logoColor=white" alt="Free">
</p>

## Features
- Live preview and drag-and-drop loading (images and videos)
- Image modes: Classic JPEG, Minecraft, Ultra-мыло
- Video modes: Classic JPEG, Minecraft, Ultra-мыло + real FFmpeg codecs: MJPEG, MPEG-2 video, MPEG-4 part 2, Snow, WMV2, FLV1, H.263+ and H.264
- Optional audio shakalization: low sample rate, mono, low bitrate and FFmpeg crush/lowpass filters
- Trim, resize/scale, target FPS, per-effect strength
- Extra filters: blur, sharpness, saturation, contrast, noise, grayscale
- Image export: JPEG, PNG, WebP (EXIF optional)
- Video export: MP4 (H.264 + AAC), MOV (H.264 + AAC), AVI (MPEG-4 + MP3), WebM (VP9 + Opus), GIF (no sound)
- Codec modes use a real FFmpeg encode/decode pass, so artifacts are produced by the codec itself
- Keep original files unchanged; works locally
- Dark UI with hover/click/slider/export sounds (QtMultimedia)

## Requirements
- FFmpeg — required for video tab. `imageio-ffmpeg` is included in `requirements.txt` (auto-provides FFmpeg). Alternatively install `ffmpeg` via `winget`/from ffmpeg.org and put `ffmpeg.exe` next to the app or on PATH.
- PySide6>=6.7, Pillow>=10.0, numpy>=1.26

## Install and run (Windows)
```bat
start bat or
py -m pip install -r requirements.txt
py shakalizer_pro.py
```

## Notes
- For video preview/playback the app uses FFmpeg streams and processes frames in Python (Pillow/NumPy). Export keeps audio only for MP4/MOV/AVI/WebM. Audio shakalization works with AAC, MP3 and Opus.
- Codec modes first encode the selected fragment with the real FFmpeg codec, then decode it back for the extra filters and final export. The first Play in a codec mode builds a short-lived low-resolution proxy, so playback can take a few seconds to start.
- SVG/logo and sounds loaded from the app folder or next to the script.
- If audio device is unavailable, sounds are disabled automatically.

<p align="center">
  <a href="https://www.pinterest.com/pin/1148136498790283379/" target="_blank">
    <img width="90" height="97" alt="shakal111111111" src="https://github.com/user-attachments/assets/53758956-7268-45c2-92bc-b9ad27a07962" />
  </a>
</p>


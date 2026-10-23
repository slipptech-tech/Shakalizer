"""
Shakalizer video editor module.

Движок: кадры декодируются FFmpeg, шакализация считается в Python (Pillow/NumPy),
готовый поток кадров кодируется обратно FFmpeg. Звук берётся из исходника.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

import numpy as np
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps
from PySide6.QtCore import QSize, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
except Exception:
    QMediaPlayer = None
    QAudioOutput = None

VIDEO_EXTS = {
    ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".webm", ".gif", ".wmv",
    ".flv", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".ogv",
}

LOGO_HEIGHT = 100
PREVIEW_LONG = 900                                                          
PLAY_LONG = 720                                                        
PLAY_FPS = 15                                                    

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_HIDDEN = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}


# ----------------------------------------------------------------------------
                         
# ----------------------------------------------------------------------------

def _works(binary: str) -> bool:
    try:
        res = subprocess.run(
            [binary, "-version"], capture_output=True, timeout=8, **_HIDDEN
        )
        return res.returncode == 0
    except Exception:
        return False


@lru_cache(maxsize=1)
def find_ffmpeg() -> Optional[str]:
    """Ищет рабочий ffmpeg.exe: переменная окружения, папка программы, PATH, imageio-ffmpeg."""
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = []

    env_bin = os.environ.get("FFMPEG_BINARY")
    if env_bin:
        candidates.append(env_bin)

    candidates += [
        os.path.join(base, "ffmpeg.exe"),
        os.path.join(base, "bin", "ffmpeg.exe"),
        shutil.which("ffmpeg") or "",
    ]

    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "")
    candidates += [
        os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
        os.path.join(program_files, "ffmpeg", "bin", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]

    try:
        import imageio_ffmpeg                                                       
        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass

    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path) and _works(path):
            return path
    return None


@lru_cache(maxsize=4)
def find_ffprobe(ffmpeg: str) -> Optional[str]:
    env_bin = os.environ.get("FFPROBE_BINARY")
    candidates = [
        env_bin or "",
        os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe"),
        shutil.which("ffprobe") or "",
    ]
    for path in candidates:
        if path and os.path.isfile(path) and _works(path):
            return path
    return None


def ffmpeg_missing_text() -> str:
    return (
        "Для работы с видео нужен FFmpeg.\n\n"
        "Варианты установки:\n"
        "1) pip install imageio-ffmpeg  (FFmpeg придёт вместе с пакетом)\n"
        "2) winget install Gyan.FFmpeg  (или скачать с ffmpeg.org)\n"
        "3) положить ffmpeg.exe рядом с shakalizer_pro.py\n\n"
        "После установки перезапустите приложение."
    )


# ----------------------------------------------------------------------------
                     
# ----------------------------------------------------------------------------

@dataclass
class VideoInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    fps_fraction: str
    has_audio: bool
    codec: str


def _fraction(value: str, fallback: float = 30.0) -> float:
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else fallback
        return float(value)
    except Exception:
        return fallback


def _pick_fps(stream: dict) -> tuple[float, str]:
    for key in ("r_frame_rate", "avg_frame_rate"):
        raw = str(stream.get(key) or "")
        fps = _fraction(raw, 0.0)
        if 0.1 <= fps <= 240.0:
            return fps, raw or f"{fps:g}"
    return 30.0, "30"


def _rotation(stream: dict) -> int:
    value = 0.0
    for item in stream.get("side_data_list") or []:
        if "rotation" in item:
            try:
                value = float(item["rotation"])
                break
            except Exception:
                pass
    if not value:
        try:
            value = float((stream.get("tags") or {}).get("rotate", 0))
        except Exception:
            value = 0.0
    return int(round(value))


def _probe_with_ffprobe(path: str, ffprobe: str) -> VideoInfo:
    cmd = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    res = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=40, **_HIDDEN,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "ffprobe завершился с ошибкой")
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("В файле не найдена видеодорожка")

    duration = 0.0
    for candidate in ((data.get("format") or {}).get("duration"), video.get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except Exception:
            pass

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    rotation = _rotation(video)
    if abs(rotation) % 180 == 90:
        width, height = height, width
    if width <= 0 or height <= 0:
        raise RuntimeError("Не удалось определить размеры видео")
    if duration <= 0:
        raise RuntimeError("Не удалось определить длительность видео")

    fps, fps_fraction = _pick_fps(video)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return VideoInfo(
        path=path, duration=duration, width=width, height=height,
        fps=fps, fps_fraction=fps_fraction, has_audio=has_audio,
        codec=str(video.get("codec_name") or ""),
    )


def _probe_with_ffmpeg(path: str, ffmpeg: str) -> VideoInfo:
                                                                                           
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", path],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=40, **_HIDDEN,
    )
    text = res.stderr or ""
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not match:
        raise RuntimeError("Не удалось прочитать длительность видео")
    duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))

    video_line = next((ln for ln in text.splitlines() if "Video:" in ln), "")
    if not video_line:
        raise RuntimeError("В файле не найдена видеодорожка")

    size = re.search(r"(\d{2,5})x(\d{2,5})", video_line)
    if not size:
        raise RuntimeError("Не удалось определить размеры видео")
    width, height = int(size.group(1)), int(size.group(2))

    fps_match = re.search(r"([\d.]+)\s*fps", video_line)
    fps = float(fps_match.group(1)) if fps_match else 30.0
    if not (0.1 <= fps <= 240.0):
        fps = 30.0

    rot = re.search(r"rotation of\s*(-?[\d.]+)\s*degrees", text)
    if rot:
        try:
            if abs(int(round(float(rot.group(1))))) % 180 == 90:
                width, height = height, width
        except Exception:
            pass

    codec_match = re.search(r"Video:\s*([A-Za-z0-9_]+)", video_line)
    codec = codec_match.group(1) if codec_match else ""
    has_audio = any("Audio:" in ln for ln in text.splitlines())
    return VideoInfo(
        path=path, duration=duration, width=width, height=height,
        fps=fps, fps_fraction=f"{fps:g}", has_audio=has_audio, codec=codec,
    )


def probe_video(path: str) -> VideoInfo:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(ffmpeg_missing_text())
    ffprobe = find_ffprobe(ffmpeg)
    if ffprobe:
        try:
            return _probe_with_ffprobe(path, ffprobe)
        except Exception:
            pass
    return _probe_with_ffmpeg(path, ffmpeg)


# ----------------------------------------------------------------------------
                            
# ----------------------------------------------------------------------------

def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def jpeg_roundtrip(img: Image.Image, quality: float) -> Image.Image:
    """Сжимает кадр в JPEG с заданным качеством и возвращает распакованный RGB."""
    import io
    buf = io.BytesIO()
    img.convert("RGB").save(
        buf, "JPEG", quality=int(max(1, min(95, quality))), subsampling=2
    )
    buf.seek(0)
    with Image.open(buf) as out:
        return out.convert("RGB").copy()


def shrink_to(img: Image.Image, long_side: int) -> Image.Image:
    w, h = img.size
    long_side = max(8, min(max(w, h), int(long_side)))
    k = long_side / max(w, h)
    return img.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)


def shrink_rel(img: Image.Image, factor: float) -> Image.Image:
    """Уменьшает кадр до доли длинной стороны (не зависит от разрешения видео)."""
    return shrink_to(img, max(8, int(round(max(img.size) * factor))))


def even(value: int) -> int:
    return max(2, int(value) // 2 * 2)


def scaled_size(width: int, height: int, max_long: int) -> tuple[int, int]:
    long_side = max(width, height)
    if long_side <= max_long:
        return max(2, width), max(2, height)
    k = max_long / long_side
    return max(2, round(width * k)), max(2, round(height * k))


def pil_to_qimage(img: Image.Image) -> QImage:
    if img.mode != "RGB":
        img = img.convert("RGB")
    data = img.tobytes("raw", "RGB")
    return QImage(data, img.width, img.height, img.width * 3, QImage.Format_RGB888).copy()


# ----------------------------------------------------------------------------
                      
# ----------------------------------------------------------------------------

def fx_classic(img: Image.Image, level: int, t: float, seed: int) -> Image.Image:
    """Классика: многократный убитый JPEG и крупные квадраты."""
    strength = level / 100.0
    w, h = img.size
    small = shrink_rel(img, lerp(0.55, 0.075, strength))
    quality = lerp(34, 1, strength ** 0.6)
    passes = 1 + int(strength * 4)

    for i in range(passes):
        dx, dy = (i * 3) % 8, (i * 5) % 8
        shifted = ImageChops.offset(small, dx, dy)
        shifted = jpeg_roundtrip(shifted, quality)
        small = ImageChops.offset(shifted, -dx, -dy)

    small = ImageEnhance.Color(small).enhance(1 + 0.8 * strength)
    return small.resize((w, h), Image.NEAREST)


def fx_minecraft(img: Image.Image, level: int, t: float, seed: int) -> Image.Image:
    """Minecraft: пиксели разных размеров, разбиение квадро-деревом.

    Зерно случайности фиксировано, поэтому рисунок блоков не мерцает от кадра к кадру.
    """
    strength = level / 100.0
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    height, width = arr.shape[:2]

    unit = max(1, round(max(width, height) * lerp(0.0010, 0.0075, strength)))
    big = 8 * unit
    sizes = [big, max(1, big // 2), max(1, big // 4), max(1, big // 8)]

    pad_h, pad_w = (-height) % big, (-width) % big
    arr = np.pad(arr, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
    pad_height, pad_width = arr.shape[:2]

    rng = np.random.default_rng(seed & 0xFFFFFFFF)
    out = np.zeros_like(arr)
    decided = None

    for index, size in enumerate(sizes):
        gh, gw = pad_height // size, pad_width // size
        cells = np.asarray(
            Image.fromarray(arr).resize((gw, gh), Image.BOX), dtype=np.uint8
        )
        if decided is None:
            decided = np.zeros((gh, gw), dtype=bool)
        else:
            decided = np.repeat(np.repeat(decided, 2, axis=0), 2, axis=1)

        if index == len(sizes) - 1:
            stop = ~decided
        else:
            stop = (~decided) & (rng.random((gh, gw)) < 0.4)
        decided = decided | stop

        cell_img = np.repeat(np.repeat(cells, size, axis=0), size, axis=1)
        mask = np.repeat(np.repeat(stop, size, axis=0), size, axis=1)
        out[mask] = cell_img[mask]

    return Image.fromarray(out[:height, :width])


def fx_ultramylo(img: Image.Image, level: int, t: float, seed: int) -> Image.Image:
    """Ультрамыло: крошечное разрешение, JPEG и сильное размытие."""
    strength = level / 100.0
    w, h = img.size
    small = shrink_rel(img, lerp(0.28, 0.035, strength))
    sw, sh = small.size
    quality = lerp(25, 3, strength)
    passes = 2 + int(strength * 3)

    for _ in range(passes):
        small = jpeg_roundtrip(small, quality)
        small = (
            small.resize((sw * 2, sh * 2), Image.BICUBIC)
            .filter(ImageFilter.GaussianBlur(0.6 + strength))
            .resize((sw, sh), Image.BILINEAR)
        )

    big = small.resize((w, h), Image.BICUBIC)
    blur_radius = max(0.5, (w / sw) * lerp(0.35, 0.6, strength))
    big = big.filter(ImageFilter.GaussianBlur(blur_radius))
    big = ImageEnhance.Contrast(big).enhance(1 - 0.2 * strength)
    return jpeg_roundtrip(big, lerp(40, 8, strength))


def apply_extras(img: Image.Image, settings: dict, t: float, seed: int) -> Image.Image:
    if settings.get("blur"):
        img = img.filter(ImageFilter.GaussianBlur(float(settings["blur"]) / 2.0))
    if settings.get("sharp", 100) != 100:
        img = ImageEnhance.Sharpness(img).enhance(float(settings["sharp"]) / 100.0)
    if settings.get("saturation", 100) != 100:
        img = ImageEnhance.Color(img).enhance(float(settings["saturation"]) / 100.0)
    if settings.get("contrast", 100) != 100:
        img = ImageEnhance.Contrast(img).enhance(float(settings["contrast"]) / 100.0)
    if settings.get("grayscale"):
        img = ImageOps.grayscale(img).convert("RGB")
    if settings.get("noise"):
        rng = np.random.default_rng((seed + int(t * 1000) * 13) & 0xFFFFFFFF)
        arr = np.asarray(img).astype(np.int16)
        sigma = float(settings["noise"]) * 0.55
        arr = np.clip(arr + rng.normal(0, sigma, arr.shape), 0, 255).astype(np.uint8)
        img = Image.fromarray(arr)
    return img


def fx_none(img: Image.Image, level: int, t: float, seed: int) -> Image.Image:
    return img


EFFECTS = {
    "classic": fx_classic,
    "minecraft": fx_minecraft,
    "ultramylo": fx_ultramylo,
    "none": fx_none,
}

                                                                                
                                                                
CODEC_TITLES = {
    "mjpeg": "MJPEG — реальный кодек FFmpeg",
    "mpeg2video": "MPEG-2 video — реальный кодек FFmpeg",
    "mpeg4": "MPEG-4 part 2 — реальный кодек FFmpeg",
    "snow": "Snow — реальный кодек FFmpeg",
    "wmv2": "WMV2 — реальный кодек FFmpeg",
    "flv": "FLV1 — реальный кодек FFmpeg",
    "h263p": "H.263+ — реальный кодек FFmpeg",
    "libx264": "H.264 — реальный кодек FFmpeg",
}

CODEC_HINTS = {
    "mjpeg": "Кадры сжимаются настоящим MJPEG: блочность, цветные ореолы и грязь по краям.",
    "mpeg2video": "Настоящий MPEG-2: макроблоки, межкадровые артефакты и «плывущая» картинка.",
    "mpeg4": "MPEG-4 part 2: грубая компрессия, квадраты и размытие движения.",
    "snow": "Экспериментальный кодек Snow: характерная «снежная» каша и сильные артефакты.",
    "wmv2": "WMV2: артефакты старого Windows Media, мягкая картинка и блочность.",
    "flv": "FLV1 (Sorenson H.263): так сжимали видео для старых Flash-плееров.",
    "h263p": "H.263+: низкий битрейт и жёсткая компрессия, как в старых мобильных видео.",
    "libx264": "H.264 с высоким CRF: современный кодек, но с убитым битрейтом.",
}

MODE_TITLES = {
    "classic": "Классическая JPEG-деградация",
    "minecraft": "Minecraft-пикселизация",
    "ultramylo": "Ультрамыло",
    **CODEC_TITLES,
}

MODE_HINTS = {
    "classic": "Многократный JPEG, крупные квадраты и убитые цвета — базовая шакализация.",
    "minecraft": "Пиксели разных размеров — крупные блоки в спокойных местах, мелкие в деталях.",
    "ultramylo": "Очень низкое разрешение, JPEG и размытие — всё превращается в кашу.",
    **CODEC_HINTS,
}


def is_codec_mode(mode: str) -> bool:
    return str(mode) in CODEC_TITLES


def codec_dims(width: int, height: int, mode: str) -> tuple[int, int]:
    """Размеры, которые гарантированно принимает выбранный кодировщик."""
    width, height = even(width), even(height)
    if mode == "h263p":
        width = max(4, width // 4 * 4)
        height = max(4, height // 4 * 4)
    return width, height


def build_codec_args(mode: str, level: int) -> list[str]:
    """Параметры кодировщика FFmpeg. Чем выше интенсивность, тем хуже качество."""
    strength = max(0.0, min(1.0, int(level) / 100.0))
    qscale = int(round(lerp(2, 31, strength)))
    if mode == "mjpeg":
        return ["-c:v", "mjpeg", "-q:v", str(qscale), "-pix_fmt", "yuvj420p"]
    if mode == "mpeg2video":
        return ["-c:v", "mpeg2video", "-q:v", str(qscale), "-pix_fmt", "yuv420p",
                "-g", "12", "-bf", "2"]
    if mode == "mpeg4":
        return ["-c:v", "mpeg4", "-q:v", str(qscale), "-pix_fmt", "yuv420p",
                "-g", "12", "-bf", "2"]
    if mode == "snow":
        return ["-c:v", "snow", "-q:v", str(qscale), "-pix_fmt", "yuv420p",
                "-g", "12", "-strict", "-2"]
    if mode == "wmv2":
        return ["-c:v", "wmv2", "-q:v", str(qscale), "-pix_fmt", "yuv420p"]
    if mode == "flv":
        return ["-c:v", "flv", "-q:v", str(qscale), "-pix_fmt", "yuv420p"]
    if mode == "h263p":
        return ["-c:v", "h263p", "-q:v", str(qscale), "-pix_fmt", "yuv420p"]
    if mode == "libx264":
        crf = int(round(lerp(18, 48, strength)))
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-pix_fmt", "yuv420p", "-g", "12"]
    return []


def apply_pipeline(img: Image.Image, settings: dict, t: float = 0.0, seed: int = 0) -> Image.Image:
    img = img.convert("RGB")
    mode = str(settings.get("mode", "classic"))
    if is_codec_mode(mode):
        fn = fx_none
    else:
        fn = EFFECTS.get(mode, fx_classic)
    out = fn(img, int(settings.get("level", 65)), float(t), int(seed))
    if out.size != img.size:
        out = out.resize(img.size, Image.BILINEAR)
    return apply_extras(out, settings, float(t), int(seed))


# ----------------------------------------------------------------------------
                                           
# ----------------------------------------------------------------------------

def decode_frame(info: VideoInfo, t: float, max_long: int = PREVIEW_LONG) -> Image.Image:
    """Достаёт один кадр из видео (уже масштабированный под предпросмотр)."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(ffmpeg_missing_text())
    t = max(0.0, min(float(t), max(0.0, info.duration - 0.04)))
    width, height = scaled_size(info.width, info.height, max_long)
    vf = f"scale={width}:{height}:flags=lanczos,setsar=1"
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{t:.3f}", "-i", info.path, "-frames:v", "1",
        "-an", "-sn", "-vf", vf,
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=60, **_HIDDEN)
    need = width * height * 3
    if res.returncode != 0 or len(res.stdout) < need:
        error = res.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(error[:600] or "FFmpeg не смог прочитать кадр")
    return Image.frombytes("RGB", (width, height), res.stdout[:need])


def codec_frame_preview(
    info: VideoInfo, t: float, mode: str, level: int,
    max_long: int = PREVIEW_LONG,
) -> Image.Image:
    """Прогоняет один кадр через настоящий кодировщик FFmpeg и декодирует обратно."""
    frame = decode_frame(info, t, max_long)
    width, height = codec_dims(frame.width, frame.height, mode)
    if frame.size != (width, height):
        frame = frame.resize((width, height), Image.LANCZOS)
    raw = frame.convert("RGB").tobytes()
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(ffmpeg_missing_text())

    enc_cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-r", "1", "-i", "-",
    ] + build_codec_args(mode, level) + [
        "-frames:v", "1", "-an", "-sn", "-f", "matroska", "-",
    ]
    enc = subprocess.run(
        enc_cmd, input=raw, capture_output=True, timeout=90, **_HIDDEN
    )
    if enc.returncode != 0 or not enc.stdout:
        error = enc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(error[:600] or "Кодировщик FFmpeg не смог сжать кадр")

    dec_cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "matroska", "-i", "-", "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    dec = subprocess.run(
        dec_cmd, input=enc.stdout, capture_output=True, timeout=90, **_HIDDEN
    )
    need = width * height * 3
    if dec.returncode != 0 or len(dec.stdout) < need:
        error = dec.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(error[:600] or "FFmpeg не смог прочитать сжатый кадр")
    return Image.frombytes("RGB", (width, height), dec.stdout[:need])


class CodecProxyBuilder(QThread):
    """Готовит низкокачественный прокси-файл выбранным кодеком для плеера."""

    finished_ok = Signal(str, int, int)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self, info: VideoInfo, mode: str, level: int, out_path: str,
        max_long: int = PLAY_LONG, fps: int = PLAY_FPS,
    ):
        super().__init__()
        self.info = info
        self.mode = mode
        self.level = int(level)
        self.out_path = out_path
        self.max_long = int(max_long)
        self.fps = int(fps)
        self._proc = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass

    def run(self):
        try:
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                raise RuntimeError(ffmpeg_missing_text())
            width, height = scaled_size(
                self.info.width, self.info.height, self.max_long
            )
            width, height = codec_dims(width, height, self.mode)
            vf = f"fps={self.fps},scale={width}:{height}:flags=lanczos,setsar=1"
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", self.info.path, "-vf", vf, "-an", "-sn",
            ] + build_codec_args(self.mode, self.level) + [
                "-f", "matroska", self.out_path,
            ]
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, **_HIDDEN,
            )
            _, err = self._proc.communicate()
            if self._cancelled:
                raise RuntimeError("cancelled")
            if self._proc.returncode != 0:
                message = (err or b"").decode("utf-8", "replace").strip()
                raise RuntimeError(message[:800] or "FFmpeg завершился с ошибкой")
            if not os.path.isfile(self.out_path) or os.path.getsize(self.out_path) == 0:
                raise RuntimeError("FFmpeg не создал прокси-файл")
            self.finished_ok.emit(self.out_path, width, height)
        except Exception as exc:
            try:
                if os.path.isfile(self.out_path):
                    os.remove(self.out_path)
            except Exception:
                pass
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))


def _read_exact(stream, size: int) -> bytes:
    chunks = []
    got = 0
    while got < size:
        block = stream.read(size - got)
        if not block:
            break
        chunks.append(block)
        got += len(block)
    return b"".join(chunks)


def _make_decoder_cmd(
    info: VideoInfo, start: float, duration: float,
    width: int, height: int, fps_arg: str, path: Optional[str] = None,
) -> list[str]:
    ffmpeg = find_ffmpeg()
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{start:.3f}", "-i", path or info.path,
        "-t", f"{duration:.3f}",
        "-vf", f"fps={fps_arg},scale={width}:{height}:flags=lanczos,setsar=1",
        "-an", "-sn", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]


AUDIO_PROFILES = {
    "mp4": ("aac", 128,
            [44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000],
            [128, 96, 80, 64, 48, 40, 32, 24, 16]),
    "mov": ("aac", 128,
            [44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000],
            [128, 96, 80, 64, 48, 40, 32, 24, 16]),
    "avi": ("libmp3lame", 128,
            [44100, 32000, 24000, 22050, 16000, 12000, 11025],
            [128, 96, 80, 64, 56, 48, 32]),
    "webm": ("libopus", 96,
             [48000, 32000, 24000, 16000, 12000, 8000],
             [96, 80, 64, 48, 32, 24, 16]),
}


def _audio_profile(fmt: str) -> tuple[str, int, list[int], list[int]]:
    return AUDIO_PROFILES.get(fmt, AUDIO_PROFILES["mp4"])


def _audio_shakal_options(fmt: str, level: int) -> tuple[str, int, int, int, str]:
    """Возвращает кодек, частоту, битрейт, каналы и фильтр для убитого звука."""
    codec, _, rates, bitrates = _audio_profile(fmt)
    strength = max(0.0, min(1.0, int(level) / 100.0))
    index = min(len(rates) - 1, int(round(strength * (len(rates) - 1))))
    rate = rates[index]
    bitrate = bitrates[min(index, len(bitrates) - 1)]
    channels = 1 if strength >= 1.0 else 2
    bits = int(round(lerp(12, 4, strength)))
    mix = lerp(0.15, 0.9, strength)
    lowpass = int(round(lerp(10000, 2600, strength)))
    highpass = int(round(lerp(80, 350, strength)))
    af = (
        f"highpass=f={highpass},lowpass=f={lowpass},"
        f"acrusher=bits={bits}:mix={mix:.2f}:mode=log"
    )
    return codec, rate, bitrate, channels, af


def _make_encoder_cmd(
    info: VideoInfo, settings: dict, width: int, height: int,
    fps_arg: str, out_path: str,
) -> list[str]:
    ffmpeg = find_ffmpeg()
    fmt = str(settings.get("format", "mp4"))
    start = float(settings.get("start", 0.0))
    duration = float(settings.get("duration", info.duration))
    audio = bool(settings.get("audio")) and info.has_audio and fmt != "gif"

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-r", fps_arg, "-i", "-",
    ]
    if audio:
        cmd += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", info.path]

    cmd += ["-map", "0:v:0"]
    if audio:
        cmd += ["-map", "1:a:0?", "-shortest"]
    else:
        cmd += ["-an"]

    quality = int(settings.get("quality", 40)) / 100.0
    if fmt in ("mp4", "mov"):
        crf = int(round(lerp(40, 16, quality)))
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                "-pix_fmt", "yuv420p"]
        if fmt == "mp4":
            cmd += ["-movflags", "+faststart"]
    elif fmt == "avi":
        qscale = int(round(lerp(31, 2, quality)))
        cmd += ["-c:v", "mpeg4", "-q:v", str(qscale), "-pix_fmt", "yuv420p",
                "-vtag", "XVID"]
    elif fmt == "webm":
        crf = int(round(lerp(55, 20, quality)))
        cmd += ["-c:v", "libvpx-vp9", "-crf", str(crf), "-b:v", "0",
                "-row-mt", "1", "-cpu-used", "3", "-pix_fmt", "yuv420p"]
    elif fmt == "gif":
        colors = int(round(lerp(32, 256, quality)))
        palette = (
            "split[a][b];[a]palettegen=max_colors=%d:stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=5" % colors
        )
        cmd += ["-vf", palette, "-loop", "0" if settings.get("gif_loop", True) else "-1"]

    if audio:
        if settings.get("audio_shakal"):
            codec, rate, bitrate, channels, af = _audio_shakal_options(
                fmt, int(settings.get("audio_level", 70))
            )
            cmd += ["-c:a", codec, "-b:a", f"{bitrate}k", "-ar", str(rate),
                    "-ac", str(channels), "-af", af]
        else:
            codec, bitrate, _, _ = _audio_profile(fmt)
            cmd += ["-c:a", codec, "-b:a", f"{bitrate}k", "-ac", "2"]

    cmd += ["-f", fmt, out_path]
    return cmd


class VideoExporter(QThread):
    progress = Signal(int)
    status = Signal(str)
    finished_ok = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self, info: VideoInfo, settings: dict, width: int, height: int,
        fps_float: float, fps_arg: str, out_path: str, seed: int,
    ):
        super().__init__()
        self.info = info
        self.settings = dict(settings)
        self.width = width
        self.height = height
        self.fps_float = max(1.0, float(fps_float))
        self.fps_arg = fps_arg
        self.out_path = out_path
        self.seed = int(seed)
        self._dec = None
        self._enc = None
        self._codec_proc = None
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        for proc in (self._dec, self._enc, self._codec_proc):
            try:
                if proc and proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

    def _cleanup(self, keep_output: bool):
        for proc in (self._enc, self._dec, self._codec_proc):
            if not proc:
                continue
            try:
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe and not getattr(pipe, "closed", True):
                        pipe.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if not keep_output:
            try:
                if os.path.isfile(self.out_path):
                    os.remove(self.out_path)
            except Exception:
                pass

    def _run_codec_pass(self, out_path: str, start: float, duration: float):
        """Готовит промежуточный файл, сжатый выбранным кодеком FFmpeg."""
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError(ffmpeg_missing_text())
        mode = str(self.settings.get("mode", ""))
        width, height = codec_dims(self.width, self.height, mode)
        vf = f"fps={self.fps_arg},scale={width}:{height}:flags=lanczos,setsar=1"
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{start:.3f}", "-i", self.info.path,
            "-t", f"{duration:.3f}", "-vf", vf, "-an", "-sn",
        ] + build_codec_args(mode, int(self.settings.get("level", 65))) + [
            "-progress", "pipe:1", "-nostats", "-f", "matroska", out_path,
        ]

        log_file = tempfile.NamedTemporaryFile(
            prefix="shakalizer_codec_", suffix=".log", delete=False
        )
        log_path = log_file.name
        log_file.close()
        last_pct = -1
        try:
            with open(log_path, "wb") as codec_err:
                self._codec_proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=codec_err, bufsize=0, **_HIDDEN,
                )
                for raw in self._codec_proc.stdout or ():
                    if self._cancelled:
                        self._codec_proc.terminate()
                        break
                    line = raw.decode("ascii", "replace").strip()
                    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                        try:
                            seconds = int(line.split("=", 1)[1]) / 1_000_000.0
                        except Exception:
                            continue
                        pct = min(34, int(seconds / max(duration, 0.001) * 34))
                        if pct != last_pct:
                            last_pct = pct
                            self.progress.emit(pct)
                self._codec_proc.wait()
            if self._cancelled:
                return
            if self._codec_proc.returncode != 0:
                raise RuntimeError(
                    _read_log(log_path)[:800] or "Кодировщик FFmpeg завершился с ошибкой"
                )
            if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
                raise RuntimeError("FFmpeg не создал промежуточный видеофайл")
        finally:
            proc = self._codec_proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            self._codec_proc = None
            try:
                if os.path.isfile(log_path):
                    os.remove(log_path)
            except Exception:
                pass

    def run(self):
        dec_log = enc_log = ""
        dec_path = enc_path = ""
        intermediate = ""
        success = False
        try:
            duration = float(self.settings.get("duration", 0.0))
            start = float(self.settings.get("start", 0.0))
            total = max(1, int(round(duration * self.fps_float)))

            codec_mode = is_codec_mode(str(self.settings.get("mode", "")))
            video_path = self.info.path
            video_start = start
            base_pct = 0
            if codec_mode:
                base_pct = 35
                self.status.emit(
                    f"Кодирование {CODEC_TITLES.get(self.settings['mode'], self.settings['mode'])}…"
                )
                self.progress.emit(0)
                fd, intermediate = tempfile.mkstemp(
                    prefix="shakalizer_codec_", suffix=".mkv"
                )
                os.close(fd)
                self._run_codec_pass(intermediate, start, duration)
                if self._cancelled:
                    self._cleanup(keep_output=False)
                    self.cancelled.emit()
                    return
                video_path = intermediate
                video_start = 0.0

            dec_cmd = _make_decoder_cmd(
                self.info, video_start, duration, self.width, self.height,
                self.fps_arg, path=video_path,
            )
            enc_cmd = _make_encoder_cmd(
                self.info, self.settings, self.width, self.height,
                self.fps_arg, self.out_path,
            )

            dec_file = tempfile.NamedTemporaryFile(
                prefix="shakalizer_dec_", suffix=".log", delete=False
            )
            enc_file = tempfile.NamedTemporaryFile(
                prefix="shakalizer_enc_", suffix=".log", delete=False
            )
            dec_path, enc_path = dec_file.name, enc_file.name
            dec_file.close()
            enc_file.close()

            with open(dec_path, "wb") as dec_err, open(enc_path, "wb") as enc_err:
                self._dec = subprocess.Popen(
                    dec_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=dec_err, bufsize=0, **_HIDDEN,
                )
                self.status.emit("Запуск кодировщика…")
                self._enc = subprocess.Popen(
                    enc_cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=enc_err, bufsize=0, **_HIDDEN,
                )

                frame_size = self.width * self.height * 3
                index = 0
                last_pct = -1
                available = max(1, 99 - base_pct)
                while not self._cancelled:
                    raw = _read_exact(self._dec.stdout, frame_size)
                    if len(raw) < frame_size:
                        break
                    frame = Image.frombytes("RGB", (self.width, self.height), raw)
                    moment = start + index / self.fps_float
                    result = apply_pipeline(frame, self.settings, moment, self.seed)
                    if result.size != (self.width, self.height):
                        result = result.resize((self.width, self.height), Image.BILINEAR)
                    try:
                        self._enc.stdin.write(result.convert("RGB").tobytes())
                    except (BrokenPipeError, OSError):
                        break
                    index += 1
                    pct = min(99, base_pct + int(index * available / total))
                    if pct != last_pct:
                        last_pct = pct
                        self.progress.emit(pct)
                    if index % 10 == 0:
                        self.status.emit(f"Экспорт: {index}/{total} кадров")

                try:
                    self._enc.stdin.close()
                except Exception:
                    pass
                try:
                    self._dec.stdout.close()
                except Exception:
                    pass
                self._enc.wait()
                self._dec.wait()

            if self._cancelled:
                self._cleanup(keep_output=False)
                self.cancelled.emit()
                return

            dec_log = _read_log(dec_path)
            enc_log = _read_log(enc_path)
            if index == 0:
                raise RuntimeError("Не удалось прочитать ни одного кадра.\n" + dec_log[:500])
            if self._enc.returncode != 0:
                raise RuntimeError(enc_log[:800] or "Кодировщик FFmpeg завершился с ошибкой")
            if not os.path.isfile(self.out_path) or os.path.getsize(self.out_path) == 0:
                raise RuntimeError("FFmpeg не создал выходной файл.\n" + enc_log[:500])

            success = True
            self.progress.emit(100)
            self.finished_ok.emit(self.out_path)
        except Exception as exc:
            if self._cancelled:
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))
        finally:
            self._cleanup(keep_output=success)
            for path in (dec_path, enc_path, intermediate):
                try:
                    if path and os.path.isfile(path):
                        os.remove(path)
                except Exception:
                    pass


def _read_log(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except Exception:
        return ""


class PreviewPlayer(QThread):
    frame_ready = Signal(QImage, float)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(
        self, info: VideoInfo, start: float, settings: dict, seed: int,
        max_long: int = PLAY_LONG, fps: int = PLAY_FPS,
    ):
        super().__init__()
        self.info = info
        self.start_time = max(0.0, float(start))
        self.settings = dict(settings)
        self.seed = int(seed)
        self.max_long = max_long
        self.fps = fps
        self._proc = None
        self._stop = False

    def stop(self):
        self._stop = True
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass

    def run(self):
        try:
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                raise RuntimeError(ffmpeg_missing_text())
            width, height = scaled_size(self.info.width, self.info.height, self.max_long)
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-ss", f"{self.start_time:.3f}", "-i", self.info.path,
                "-vf", f"fps={self.fps},scale={width}:{height}:flags=lanczos,setsar=1",
                "-an", "-sn", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ]
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0, **_HIDDEN,
            )
            frame_size = width * height * 3
            index = 0
            clock = time.perf_counter()
            while not self._stop:
                raw = _read_exact(self._proc.stdout, frame_size)
                if len(raw) < frame_size:
                    break
                frame = Image.frombytes("RGB", (width, height), raw)
                moment = self.start_time + index / self.fps
                result = apply_pipeline(frame, self.settings, moment, self.seed)
                if self._stop:
                    break
                self.frame_ready.emit(pil_to_qimage(result), moment)
                index += 1
                delay = clock + index / self.fps - time.perf_counter()
                if delay > 0:
                    time.sleep(min(delay, 0.25))
            try:
                self._proc.wait(timeout=5)
            except Exception:
                pass
            if not self._stop:
                self.finished_ok.emit()
        except Exception as exc:
            if not self._stop:
                self.failed.emit(str(exc))


# ----------------------------------------------------------------------------
            
# ----------------------------------------------------------------------------

class VideoPreviewLabel(QLabel):
    def __init__(self, page: "VideoEditorPage"):
        super().__init__("Откройте видео или перетащите его сюда")
        self.page = page
        self.setObjectName("preview")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(380, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAcceptDrops(True)
        self._pix = None

    def set_image(self, pix: QPixmap):
        self._pix = pix
        self._scale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scale()

    def _scale(self):
        if self._pix and not self._pix.isNull():
            self.setPixmap(
                self._pix.scaled(
                    self.size() - QSize(24, 24),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and os.path.isfile(path):
                window = self.window()
                if hasattr(window, "load_media"):
                    window.load_media(path)
                else:
                    self.page.load_video(path)
                event.acceptProposedAction()
                return


class Panel(QFrame):
    def __init__(self, title: str, hint: str = ""):
        super().__init__()
        self.setObjectName("panel")
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(15, 14, 15, 15)
        self.box.setSpacing(10)
        label = QLabel(title)
        label.setObjectName("panelTitle")
        self.box.addWidget(label)
        if hint:
            sub = QLabel(hint)
            sub.setObjectName("muted")
            sub.setWordWrap(True)
            self.box.addWidget(sub)


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    minutes = int(seconds // 60)
    return f"{minutes:02d}:{seconds - minutes * 60:04.1f}"


class VideoEditorPage(QWidget):
    def __init__(self, parent=None, sounds=None, logo_pixmap: Optional[QPixmap] = None):
        super().__init__(parent)
        self.setObjectName("page")
        self.sounds = sounds
        self.logo_pixmap = logo_pixmap
        self._audio_output = None
        self._audio_player = None
        if QMediaPlayer is not None and QAudioOutput is not None:
            try:
                self._audio_output = QAudioOutput(self)
                self._audio_output.setVolume(1.0)
                self._audio_player = QMediaPlayer(self)
                self._audio_player.setAudioOutput(self._audio_output)
            except Exception:
                self._audio_output = None
                self._audio_player = None
        self.info: Optional[VideoInfo] = None
        self._seed = random.randrange(1, 1 << 30)
        self._play_thread: Optional[PreviewPlayer] = None
        self._export_thread: Optional[VideoExporter] = None
        self._proxy_thread: Optional[CodecProxyBuilder] = None
        self._proxy_path = ""
        self._proxy_info: Optional[VideoInfo] = None
        self._proxy_key = None
        self._proxy_pending_key = None
        self._pending_play = False
        self._codec_frame_cache: Optional[tuple] = None
        self._frame_timer = QTimer(self)
        self._frame_timer.setSingleShot(True)
        self._frame_timer.setInterval(130)
        self._frame_timer.timeout.connect(self.render_current_frame)
        self._suspend = False
        self._muted_sliders = False
        self._updating_slider = False
        self._show_original = False
        self._last_format = "mp4"
        self._last_pct = -1
        self._video_export_success = None
        try:
            if QSoundEffect is not None:
                snd_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "videoexportsuccess.wav",
                )
                if os.path.isfile(snd_path):
                    eff = QSoundEffect(self)
                    eff.setSource(QUrl.fromLocalFile(snd_path))
                    eff.setVolume(1.0)
                    self._video_export_success = eff
        except Exception:
            pass
        self._build_ui()
        self._refresh_ffmpeg_status()
        self._update_mode_hint()
        self._update_format_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 10, 0, 0)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.addWidget(self._make_logo())
        header.addStretch()
        self.open_btn = QPushButton("Открыть видео")
        self.open_btn.setObjectName("primary")
        self.open_btn.clicked.connect(self.open_video)
        header.addWidget(self.open_btn)
        self.export_btn = QPushButton("Экспортировать")
        self.export_btn.setObjectName("primary")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export_video)
        header.addWidget(self.export_btn)
        root.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(15)
        root.addLayout(body, 1)

        left = QVBoxLayout()
        preview_panel = QFrame()
        preview_panel.setObjectName("previewPanel")
        pv = QVBoxLayout(preview_panel)
        pv.setContentsMargins(14, 14, 14, 14)
        pv.setSpacing(10)

        pvhead = QHBoxLayout()
        title = QLabel("Предпросмотр видео")
        title.setObjectName("panelTitle")
        pvhead.addWidget(title)
        pvhead.addStretch()
        self.state = QLabel("ОЖИДАНИЕ")
        self.state.setObjectName("pill")
        pvhead.addWidget(self.state)
        pv.addLayout(pvhead)

        self.preview = VideoPreviewLabel(self)
        pv.addWidget(self.preview, 1)

        transport = QHBoxLayout()
        transport.setSpacing(9)
        self.play_btn = QPushButton("▶")
        self.play_btn.setObjectName("transport")
        self.play_btn.setFixedWidth(44)
        self.play_btn.setCheckable(True)
        self.play_btn.setEnabled(False)
        self.play_btn.toggled.connect(self.toggle_play)
        transport.addWidget(self.play_btn)

        self.timeline = QSlider(Qt.Horizontal)
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.valueChanged.connect(self._timeline_changed)
        self.timeline.sliderPressed.connect(self._timeline_pressed)
        transport.addWidget(self.timeline, 1)

        self.time_label = QLabel("00:00.0 / 00:00.0")
        self.time_label.setObjectName("muted")
        self.time_label.setMinimumWidth(110)
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        transport.addWidget(self.time_label)
        pv.addLayout(transport)

        info_row = QHBoxLayout()
        self.dim_label = QLabel("Размер: —")
        self.dim_label.setObjectName("muted")
        self.file_label = QLabel("Файл не выбран")
        self.file_label.setObjectName("muted")
        info_row.addWidget(self.dim_label)
        info_row.addStretch()
        info_row.addWidget(self.file_label)
        pv.addLayout(info_row)
        left.addWidget(preview_panel, 1)

        actions = QHBoxLayout()
        self.compare = QPushButton("Показать оригинал")
        self.compare.setCheckable(True)
        self.compare.setEnabled(False)
        self.compare.toggled.connect(self.toggle_original)
        actions.addWidget(self.compare)
        self.reset_btn = QPushButton("Сбросить параметры")
        self.reset_btn.clicked.connect(self.reset_settings)
        actions.addWidget(self.reset_btn)
        actions.addStretch()
        left.addLayout(actions)
        body.addLayout(left, 7)

        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.NoFrame)
        self.settings_scroll.setMinimumWidth(330)
        self.settings_scroll.setMaximumWidth(410)
        host = QWidget()
        host.setObjectName("settingsHost")
        side = QVBoxLayout(host)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(11)

        mode_panel = Panel(
            "Режим шакализации",
            "Python-эффект или настоящий кодировщик FFmpeg.",
        )
        self.mode = QComboBox()
        for key, label in MODE_TITLES.items():
            self.mode.addItem(label, key)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        mode_panel.box.addWidget(self.mode)
        self.mode_hint = QLabel()
        self.mode_hint.setObjectName("muted")
        self.mode_hint.setWordWrap(True)
        mode_panel.box.addWidget(self.mode_hint)
        self.level, self.level_label = self.add_slider(
            mode_panel, "Интенсивность", 1, 100, 65, "%"
        )
        self.level.valueChanged.connect(self._invalidate_proxy)
        side.addWidget(mode_panel)

        trim_panel = Panel("Обрезка", "Экспортируется только выбранный отрезок.")
        trim_grid = QGridLayout()
        trim_grid.setHorizontalSpacing(7)
        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(0.0, 0.0)
        self.start_spin.setDecimals(2)
        self.start_spin.setSingleStep(0.1)
        self.start_spin.setSuffix(" с")
        self.start_spin.setKeyboardTracking(False)
        self.start_spin.valueChanged.connect(self._trim_changed)
        trim_grid.addWidget(QLabel("Начало"), 0, 0)
        trim_grid.addWidget(self.start_spin, 0, 1)
        start_here = QPushButton("= кадр")
        start_here.setFixedWidth(70)
        start_here.clicked.connect(self._set_start_here)
        trim_grid.addWidget(start_here, 0, 2)

        self.end_spin = QDoubleSpinBox()
        self.end_spin.setRange(0.0, 0.0)
        self.end_spin.setDecimals(2)
        self.end_spin.setSingleStep(0.1)
        self.end_spin.setSuffix(" с")
        self.end_spin.setKeyboardTracking(False)
        self.end_spin.valueChanged.connect(self._trim_changed)
        trim_grid.addWidget(QLabel("Конец"), 1, 0)
        trim_grid.addWidget(self.end_spin, 1, 1)
        end_here = QPushButton("= кадр")
        end_here.setFixedWidth(70)
        end_here.clicked.connect(self._set_end_here)
        trim_grid.addWidget(end_here, 1, 2)
        trim_panel.box.addLayout(trim_grid)
        self.trim_label = QLabel("Длительность: —")
        self.trim_label.setObjectName("muted")
        trim_panel.box.addWidget(self.trim_label)
        side.addWidget(trim_panel)

        size_panel = Panel("Размер и частота кадров", "100% — исходное разрешение.")
        self.scale, self.scale_label = self.add_slider(
            size_panel, "Масштаб", 10, 100, 100, "%", preview=False
        )
        self.custom_size = QCheckBox("Задать размер вручную")
        self.custom_size.toggled.connect(self.toggle_custom_size)
        size_panel.box.addWidget(self.custom_size)
        dims = QGridLayout()
        dims.addWidget(QLabel("Ширина"), 0, 0)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(16, 7680)
        self.width_spin.setValue(1280)
        self.width_spin.setEnabled(False)
        self.width_spin.valueChanged.connect(self._update_export_info)
        dims.addWidget(self.width_spin, 0, 1)
        dims.addWidget(QLabel("Высота"), 1, 0)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(16, 7680)
        self.height_spin.setValue(720)
        self.height_spin.setEnabled(False)
        self.height_spin.valueChanged.connect(self._update_export_info)
        dims.addWidget(self.height_spin, 1, 1)
        size_panel.box.addLayout(dims)
        self.keep_ratio = QCheckBox("Сохранять пропорции")
        self.keep_ratio.setChecked(True)
        self.keep_ratio.toggled.connect(self._update_export_info)
        size_panel.box.addWidget(self.keep_ratio)

        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("FPS"))
        self.fps_combo = QComboBox()
        for label, value in (
            ("Как в источнике", "source"),
            ("60", "60"), ("30", "30"), ("24", "24"), ("20", "20"),
            ("15", "15"), ("12", "12"), ("10", "10"), ("8", "8"),
        ):
            self.fps_combo.addItem(label, value)
        self.fps_combo.currentIndexChanged.connect(self._update_export_info)
        fps_row.addWidget(self.fps_combo, 1)
        size_panel.box.addLayout(fps_row)
        self.size_label = QLabel("Выход: —")
        self.size_label.setObjectName("muted")
        size_panel.box.addWidget(self.size_label)
        side.addWidget(size_panel)

        effects = Panel("Дополнительные эффекты", "Применяются после основного алгоритма.")
        self.blur, self.blur_label = self.add_slider(effects, "Размытие", 0, 10, 0, "")
        self.sharp, self.sharp_label = self.add_slider(effects, "Резкость", 0, 250, 100, "%")
        self.saturation, self.saturation_label = self.add_slider(
            effects, "Насыщенность", 0, 200, 100, "%"
        )
        self.contrast, self.contrast_label = self.add_slider(
            effects, "Контраст", 50, 200, 100, "%"
        )
        self.noise, self.noise_label = self.add_slider(effects, "Цифровой шум", 0, 60, 0, "%")
        self.grayscale = QCheckBox("Чёрно-белый режим")
        self.grayscale.stateChanged.connect(self.schedule_preview)
        effects.box.addWidget(self.grayscale)
        side.addWidget(effects)

        export = Panel("Экспорт", "Формат, качество и звук.")
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("Формат"))
        self.format = QComboBox()
        for label, value in (
            ("MP4 — H.264 + AAC", "mp4"),
            ("AVI — MPEG-4 + MP3", "avi"),
            ("MOV — H.264 + AAC", "mov"),
            ("WebM — VP9 + Opus", "webm"),
            ("GIF — без звука", "gif"),
        ):
            self.format.addItem(label, value)
        self.format.currentIndexChanged.connect(self._format_changed)
        fmt_row.addWidget(self.format, 1)
        export.box.addLayout(fmt_row)
        self.quality, self.quality_label = self.add_slider(
            export, "Качество файла", 1, 100, 40, "%", preview=False
        )
        self.audio = QCheckBox("Сохранить звук")
        self.audio.setChecked(True)
        self.audio.toggled.connect(self._update_audio_ui)
        export.box.addWidget(self.audio)
        self.audio_shakal = QCheckBox("Шакализация звука")
        self.audio_shakal.setToolTip(
            "Низкая частота дискретизации, моно, низкий битрейт и цифровые искажения."
        )
        self.audio_shakal.toggled.connect(self._update_audio_ui)
        export.box.addWidget(self.audio_shakal)
        self.audio_level, self.audio_level_label = self.add_slider(
            export, "Сила шакализации звука", 1, 100, 70, "%", preview=False
        )
        self.audio_hint = QLabel(
            "Звук уродуется фильтрами FFmpeg: acrusher, lowpass и высокая компрессия."
        )
        self.audio_hint.setObjectName("muted")
        self.audio_hint.setWordWrap(True)
        export.box.addWidget(self.audio_hint)
        self.gif_loop = QCheckBox("Зациклить GIF")
        self.gif_loop.setChecked(True)
        export.box.addWidget(self.gif_loop)
        self.export_info = QLabel("Выход: —")
        self.export_info.setObjectName("muted")
        self.export_info.setWordWrap(True)
        export.box.addWidget(self.export_info)
        self.format_hint = QLabel()
        self.format_hint.setObjectName("muted")
        self.format_hint.setWordWrap(True)
        export.box.addWidget(self.format_hint)
        self.tool_label = QLabel("FFmpeg: проверка…")
        self.tool_label.setObjectName("muted")
        self.tool_label.setWordWrap(True)
        export.box.addWidget(self.tool_label)
        side.addWidget(export)
        side.addStretch()
        self.settings_scroll.setWidget(host)
        body.addWidget(self.settings_scroll, 3)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(3)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.hide()
        root.addWidget(self.progress)

        footer_row = QHBoxLayout()
        self.footer = QLabel("Готово. FFmpeg ещё не найден — проверьте вкладку «Видео».")
        self.footer.setObjectName("muted")
        footer_row.addWidget(self.footer, 1)
        self.cancel_btn = QPushButton("Отмена")
        self.cancel_btn.hide()
        self.cancel_btn.clicked.connect(self.cancel_export)
        footer_row.addWidget(self.cancel_btn)
        root.addLayout(footer_row)

    def _make_logo(self):
        label = QLabel()
        label.setObjectName("logo")
        if self.logo_pixmap is None or self.logo_pixmap.isNull():
            label.setText("SHAKALIZER VIDEO")
            label.setObjectName("brand")
            return label
        dpr = self.devicePixelRatioF()
        scaled = self.logo_pixmap.scaledToHeight(
            int(LOGO_HEIGHT * dpr), Qt.SmoothTransformation
        )
        scaled.setDevicePixelRatio(dpr)
        label.setPixmap(scaled)
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        return label

    def add_slider(
        self, panel: Panel, title: str, lo: int, hi: int, value: int,
        suffix: str, preview: bool = True, sound: bool = True,
    ):
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        top = QHBoxLayout()
        name = QLabel(title)
        name.setObjectName("controlLabel")
        value_label = QLabel(f"{value}{suffix}")
        value_label.setObjectName("value")
        top.addWidget(name)
        top.addStretch()
        top.addWidget(value_label)
        layout.addLayout(top)

        slider = QSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(value)
        slider.valueChanged.connect(
            lambda v, lab=value_label, suf=suffix: lab.setText(f"{v}{suf}")
        )
        if preview:
            slider.valueChanged.connect(self.schedule_preview)
        if sound:
            slider.valueChanged.connect(self._slider_sound)
        layout.addWidget(slider)
        panel.box.addWidget(wrap)
        return slider, value_label

    def _slider_sound(self, *_):
        if not self._muted_sliders and self.sounds is not None:
            self.sounds.play_slider()

    def _refresh_ffmpeg_status(self):
        if find_ffmpeg():
            self.tool_label.setText("FFmpeg: найден")
            self.footer.setText("Готово. Обработка выполняется локально.")
        else:
            self.tool_label.setText("FFmpeg: не найден")
            self.footer.setText("FFmpeg не найден — видео откроется после установки FFmpeg.")

                                                                            
    def _mode_changed(self, *_):
        self._invalidate_proxy()
        self._update_mode_hint()
        self.schedule_preview()

    def _update_mode_hint(self):
        self.mode_hint.setText(MODE_HINTS.get(self.mode.currentData(), ""))

    def _update_audio_ui(self, *_):
        fmt = self.format.currentData() or "mp4"
        audio_ok = (
            bool(self.info and self.info.has_audio)
            and fmt != "gif"
            and self.audio.isChecked()
        )
        self.audio_shakal.setEnabled(audio_ok)
        shakal_on = audio_ok and self.audio_shakal.isChecked()
        self.audio_level.setEnabled(shakal_on)
        self.audio_hint.setVisible(shakal_on)

    def _require_ffmpeg(self) -> bool:
        if find_ffmpeg():
            return True
        QMessageBox.critical(self, "FFmpeg не найден", ffmpeg_missing_text())
        self.footer.setText("FFmpeg не найден.")
        return False

    def _effects_snapshot(self) -> dict:
        return {
            "mode": self.mode.currentData() or "classic",
            "level": self.level.value(),
            "blur": self.blur.value(),
            "sharp": self.sharp.value(),
            "saturation": self.saturation.value(),
            "contrast": self.contrast.value(),
            "noise": self.noise.value(),
            "grayscale": self.grayscale.isChecked(),
        }

    def _target_size(self) -> Optional[tuple[int, int]]:
        if not self.info:
            return None
        if self.custom_size.isChecked():
            box_w, box_h = self.width_spin.value(), self.height_spin.value()
            if self.keep_ratio.isChecked():
                ratio = min(box_w / self.info.width, box_h / self.info.height)
                width = max(2, int(round(self.info.width * ratio)))
                height = max(2, int(round(self.info.height * ratio)))
            else:
                width, height = box_w, box_h
        else:
            scale = self.scale.value() / 100.0
            width = max(2, int(round(self.info.width * scale)))
            height = max(2, int(round(self.info.height * scale)))
        return even(width), even(height)

    def _target_fps(self) -> tuple[float, str]:
        if not self.info:
            return 30.0, "30"
        key = self.fps_combo.currentData() or "source"
        if key == "source":
            return self.info.fps, self.info.fps_fraction
        try:
            fps = float(key)
        except Exception:
            return self.info.fps, self.info.fps_fraction
        return fps, str(int(fps)) if float(fps).is_integer() else f"{fps:g}"

    def _update_export_info(self, *_):
        if not self.info:
            self.size_label.setText("Выход: —")
            self.export_info.setText("Выход: —")
            return
        size = self._target_size()
        if not size:
            return
        width, height = size
        fps, _ = self._target_fps()
        duration = max(0.0, self.end_spin.value() - self.start_spin.value())
        self.size_label.setText(f"Выход: {width} × {height}")
        self.export_info.setText(
            f"Выход: {width} × {height}, {fps:g} fps, {duration:.2f} с"
        )

    def _format_changed(self, *_):
        fmt = self.format.currentData() or "mp4"
        if fmt == "gif" and self.fps_combo.currentData() == "source":
            index = self.fps_combo.findData("12")
            if index >= 0:
                self.fps_combo.setCurrentIndex(index)
        elif fmt != "gif" and self._last_format == "gif":
            if self.fps_combo.currentData() == "12":
                index = self.fps_combo.findData("source")
                if index >= 0:
                    self.fps_combo.setCurrentIndex(index)
        self._last_format = fmt
        self._update_format_ui()
        self._update_export_info()

    def _update_format_ui(self):
        fmt = self.format.currentData() or "mp4"
        has_audio = bool(self.info and self.info.has_audio)
        self.audio.setEnabled(has_audio and fmt != "gif")
        self.gif_loop.setEnabled(fmt == "gif")
        hints = {
            "mp4": "MP4: H.264 + AAC. Универсальный вариант для соцсетей и плееров.",
            "avi": "AVI: MPEG-4 + MP3. Старый контейнер, зато открывается везде.",
            "mov": "MOV: H.264 + AAC. Формат для Apple и монтажных программ.",
            "webm": "WebM: VP9 + Opus. Для браузеров; кодируется заметно медленнее.",
            "gif": "GIF: без звука. Качество = число цветов; лучше 50% и 12 fps.",
        }
        self.format_hint.setText(hints.get(fmt, ""))
        self._update_audio_ui()

    def _timeline_pressed(self):
        if self._play_thread:
            self.stop_playback()

    def _timeline_changed(self, value: int):
        if self._suspend:
            return
        self._update_time_label(value / 1000.0)
        if self._updating_slider:
            return
        if self._play_thread:
            self.stop_playback()
        self.schedule_preview()

    def _update_time_label(self, t: float):
        duration = self.info.duration if self.info else 0.0
        self.time_label.setText(f"{_fmt_time(t)} / {_fmt_time(duration)}")

                                                                             
    def _set_spin(self, spin: QDoubleSpinBox, value: float):
        spin.blockSignals(True)
        spin.setValue(max(spin.minimum(), min(spin.maximum(), value)))
        spin.blockSignals(False)

    def _trim_changed(self, *_):
        if self._suspend or not self.info:
            return
        start = self.start_spin.value()
        end = self.end_spin.value()
        if end <= start + 0.05:
            if self.sender() is self.start_spin:
                self._set_spin(self.end_spin, min(self.info.duration, start + 0.1))
            else:
                self._set_spin(self.start_spin, max(0.0, end - 0.1))
        self._update_trim_label()
        self._update_export_info()

    def _update_trim_label(self):
        if not self.info:
            self.trim_label.setText("Длительность: —")
            return
        length = max(0.0, self.end_spin.value() - self.start_spin.value())
        self.trim_label.setText(f"Длительность: {length:.2f} с")

    def _set_start_here(self):
        if not self.info:
            return
        t = self.timeline.value() / 1000.0
        self._set_spin(self.start_spin, t)
        if self.end_spin.value() <= t + 0.05:
            self._set_spin(self.end_spin, min(self.info.duration, t + 0.1))
        self._update_trim_label()
        self._update_export_info()

    def _set_end_here(self):
        if not self.info:
            return
        t = self.timeline.value() / 1000.0
        self._set_spin(self.end_spin, t)
        if self.start_spin.value() >= t - 0.05:
            self._set_spin(self.start_spin, max(0.0, t - 0.1))
        self._update_trim_label()
        self._update_export_info()

    def toggle_custom_size(self, checked: bool):
        self.width_spin.setEnabled(checked)
        self.height_spin.setEnabled(checked)
        self._update_export_info()

                                                                         
    def open_video(self):
        if not self._require_ffmpeg():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Открыть видео", "",
            "Видео (*.mp4 *.m4v *.mov *.avi *.mkv *.webm *.gif *.wmv *.flv "
            "*.ts *.mts *.m2ts *.mpg *.mpeg *.3gp *.ogv);;Все файлы (*)",
        )
        if path:
            self.load_video(path)

    def load_video(self, path: str):
        if not self._require_ffmpeg():
            return
        self._invalidate_proxy()
        self._frame_timer.stop()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            info = probe_video(path)
            if info.duration <= 0:
                raise RuntimeError("Не удалось определить длительность видео")
            self.info = info
            if self._audio_player is not None:
                self._audio_player.stop()
                self._audio_player.setSource(QUrl.fromLocalFile(os.path.abspath(path)))
            self._seed = random.randrange(1, 1 << 30)

            self.compare.blockSignals(True)
            self.compare.setChecked(False)
            self.compare.blockSignals(False)
            self.compare.setText("Показать оригинал")
            self._show_original = False

            duration_ms = max(1, int(round(info.duration * 1000)))
            self.timeline.setRange(0, duration_ms)
            self._suspend = True
            try:
                self.timeline.setValue(0)
            finally:
                self._suspend = False

            self.start_spin.setRange(0.0, info.duration)
            self.end_spin.setRange(0.0, info.duration)
            self._set_spin(self.start_spin, 0.0)
            self._set_spin(self.end_spin, info.duration)

            self.width_spin.blockSignals(True)
            self.height_spin.blockSignals(True)
            self.width_spin.setValue(info.width)
            self.height_spin.setValue(info.height)
            self.width_spin.blockSignals(False)
            self.height_spin.blockSignals(False)
            self.custom_size.setChecked(False)

            source_index = self.fps_combo.findData("source")
            if source_index >= 0:
                self.fps_combo.setCurrentIndex(source_index)

            fmt = self.format.currentData() or "mp4"
            self.audio.setEnabled(info.has_audio and fmt != "gif")
            self.audio.setChecked(info.has_audio)
            self._update_audio_ui()

            self.file_label.setText(os.path.basename(path))
            self.dim_label.setText(
                f"{info.width} × {info.height} · {info.fps:.2f} fps · {info.duration:.1f} с"
            )
            self.state.setText("ВИДЕО ЗАГРУЖЕНО")
            self.footer.setText(f"Загружено: {os.path.basename(path)}")
            self.play_btn.setEnabled(True)
            self.timeline.setEnabled(True)
            self.compare.setEnabled(True)
            self.export_btn.setEnabled(True)

            self._update_trim_label()
            self._update_export_info()
            self._update_time_label(0.0)
            self.render_current_frame()
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось открыть видео", str(exc))
            self.footer.setText(f"Ошибка: {exc}")
        finally:
            QApplication.restoreOverrideCursor()

    def load_dropped(self, path: str):
        ext = os.path.splitext(path)[1].lower()
        window = self.window()
        if hasattr(window, "load_media"):
            window.load_media(path)
        elif ext in VIDEO_EXTS:
            self.load_video(path)
        else:
            QMessageBox.information(self, "Это не видео", "Перетащите видеофайл.")

                                                                             
    def schedule_preview(self, *_):
        if (
            self._suspend or not self.info
            or self._export_thread is not None
            or self._proxy_thread is not None
        ):
            return
        if self._play_thread is not None:
            self.stop_playback()
        self._frame_timer.start()

    def _current_proxy_key(self):
        if not self.info:
            return None
        mode = self.mode.currentData() or ""
        if not is_codec_mode(mode):
            return None
        try:
            mtime = os.path.getmtime(self.info.path)
        except OSError:
            mtime = 0.0
        return (
            os.path.abspath(self.info.path), mtime, mode,
            self.level.value(), PLAY_LONG, PLAY_FPS,
        )

    def _proxy_is_valid(self) -> bool:
        key = self._current_proxy_key()
        return bool(
            key and self._proxy_key == key and self._proxy_info
            and self._proxy_path and os.path.isfile(self._proxy_path)
        )

    def _codec_base_frame(self, t: float) -> Image.Image:
        mode = str(self.mode.currentData() or "")
        key = self._current_proxy_key()
        cache_key = (key, mode, self.level.value(), round(float(t), 2))
        if self._codec_frame_cache and self._codec_frame_cache[0] == cache_key:
            return self._codec_frame_cache[1]
        if self._proxy_is_valid():
            frame = decode_frame(self._proxy_info, t, PREVIEW_LONG)
        else:
            frame = codec_frame_preview(
                self.info, t, mode, self.level.value(), PREVIEW_LONG
            )
        self._codec_frame_cache = (cache_key, frame)
        return frame

    def render_current_frame(self):
        if (
            not self.info or self._export_thread is not None
            or self._play_thread is not None or self._proxy_thread is not None
        ):
            return
        t = self.timeline.value() / 1000.0
        try:
            snapshot = self._effects_snapshot()
            if self._show_original:
                frame = decode_frame(self.info, t, PREVIEW_LONG)
            elif is_codec_mode(str(snapshot["mode"])):
                frame = self._codec_base_frame(t)
            else:
                frame = decode_frame(self.info, t, PREVIEW_LONG)
            if not self._show_original:
                frame = apply_pipeline(frame, snapshot, t, self._seed)
            self.preview.set_image(QPixmap.fromImage(pil_to_qimage(frame)))
            self._update_time_label(t)
            if self._show_original:
                self.state.setText("ОРИГИНАЛ")
            else:
                self.state.setText("ПРЕДПРОСМОТР")
            suffix = ""
            if not self._show_original and is_codec_mode(str(snapshot["mode"])):
                suffix = " · настоящий кодек FFmpeg"
            self.footer.setText(f"Предпросмотр: {_fmt_time(t)}{suffix}")
        except Exception as exc:
            self.state.setText("ОШИБКА")
            self.footer.setText(f"Ошибка предпросмотра: {exc}")

    def toggle_original(self, checked: bool):
        if not self.info:
            self.compare.blockSignals(True)
            self.compare.setChecked(False)
            self.compare.blockSignals(False)
            return
        if self._play_thread is not None:
            self.stop_playback()
        self._show_original = checked
        self.compare.setText("Показать результат" if checked else "Показать оригинал")
        self.render_current_frame()

                                                                              
    def _set_play_checked(self, checked: bool):
        self.play_btn.blockSignals(True)
        self.play_btn.setChecked(checked)
        self.play_btn.blockSignals(False)

    def _set_proxy_busy(self, busy: bool):
        self.settings_scroll.setEnabled(not busy)
        self.open_btn.setEnabled(not busy)
        has_video = self.info is not None
        self.export_btn.setEnabled(not busy and has_video)
        self.play_btn.setEnabled(not busy and has_video)
        self.timeline.setEnabled(not busy and has_video)
        self.compare.setEnabled(not busy and has_video)
        self.reset_btn.setEnabled(not busy)
        self.cancel_btn.setVisible(busy)
        self.progress.setVisible(busy)
        self.progress.setRange(0, 0 if busy else 100)

    def _start_proxy_build(self):
        if self._proxy_thread is not None:
            return
        key = self._current_proxy_key()
        if not key:
            self._start_playback()
            return

        old = self._proxy_path
        self._proxy_path = ""
        self._proxy_info = None
        self._proxy_key = None
        self._codec_frame_cache = None
        if old:
            try:
                if os.path.isfile(old):
                    os.remove(old)
            except Exception:
                pass

        fd, path = tempfile.mkstemp(prefix="shakalizer_proxy_", suffix=".mkv")
        os.close(fd)
        self._proxy_path = path
        self._proxy_pending_key = key
        self._pending_play = True
        mode = str(self.mode.currentData() or "")
        thread = CodecProxyBuilder(
            self.info, mode, self.level.value(), path,
            max_long=PLAY_LONG, fps=PLAY_FPS,
        )
        thread.finished_ok.connect(self._on_proxy_finished)
        thread.failed.connect(self._on_proxy_failed)
        thread.cancelled.connect(self._on_proxy_cancelled)
        self._proxy_thread = thread
        self._set_play_checked(True)
        self._set_proxy_busy(True)
        self.play_btn.setText("⌛")
        self.state.setText("ПОДГОТОВКА ПРОКСИ")
        self.footer.setText(
            f"Готовим прокси кодеком {CODEC_TITLES.get(mode, mode)}. "
            "Это может занять несколько секунд…"
        )
        thread.start()

    def _on_proxy_finished(self, path: str, width: int, height: int):
        thread = self._proxy_thread
        if thread is None or self.sender() is not thread:
            return
        self._proxy_thread = None
        thread.deleteLater()
        key = self._proxy_pending_key
        self._proxy_pending_key = None
        self._set_proxy_busy(False)

        current = self._current_proxy_key()
        if not key or key != current or not self.info or not os.path.isfile(path):
            self._proxy_path = ""
            self._proxy_info = None
            self._proxy_key = None
            self._pending_play = False
            self._set_play_checked(False)
            self.play_btn.setText("▶")
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass
            self.state.setText("ОШИБКА")
            self.footer.setText("Прокси устарел или не создан. Попробуйте ещё раз.")
            return

        self._proxy_path = path
        self._proxy_key = key
        self._proxy_info = VideoInfo(
            path=path, duration=self.info.duration, width=int(width),
            height=int(height), fps=float(PLAY_FPS),
            fps_fraction=str(PLAY_FPS), has_audio=False,
            codec=str(self.mode.currentData() or ""),
        )
        self._codec_frame_cache = None
        if self._pending_play:
            self._pending_play = False
            self._start_playback()
        else:
            self.render_current_frame()

    def _on_proxy_failed(self, message: str):
        thread = self._proxy_thread
        if thread is None or self.sender() is not thread:
            return
        self._proxy_thread = None
        thread.deleteLater()
        self._proxy_pending_key = None
        self._proxy_path = ""
        self._proxy_info = None
        self._proxy_key = None
        self._pending_play = False
        self._codec_frame_cache = None
        self._set_proxy_busy(False)
        self._set_play_checked(False)
        self.play_btn.setText("▶")
        self.state.setText("ОШИБКА")
        self.footer.setText("Не удалось подготовить прокси-файл.")
        QMessageBox.critical(self, "Ошибка кодека", message[:1600])

    def _on_proxy_cancelled(self):
        thread = self._proxy_thread
        if thread is None or self.sender() is not thread:
            return
        self._proxy_thread = None
        thread.deleteLater()
        self._proxy_pending_key = None
        self._proxy_path = ""
        self._proxy_info = None
        self._proxy_key = None
        self._pending_play = False
        self._set_proxy_busy(False)
        self._set_play_checked(False)
        self.play_btn.setText("▶")
        self.state.setText("ОТМЕНЕНО")
        self.footer.setText("Подготовка прокси отменена.")

    def _invalidate_proxy(self, *_):
        self._pending_play = False
        if self._play_thread is not None:
            self.stop_playback()
        thread = self._proxy_thread
        if thread is not None:
            self._proxy_thread = None
            for signal in (thread.finished_ok, thread.failed, thread.cancelled):
                try:
                    signal.disconnect()
                except Exception:
                    pass
            thread.cancel()
            if not thread.wait(5000):
                try:
                    thread.terminate()
                    thread.wait(1000)
                except Exception:
                    pass
            thread.deleteLater()
            self._set_proxy_busy(False)
        path = self._proxy_path
        self._proxy_path = ""
        self._proxy_info = None
        self._proxy_key = None
        self._proxy_pending_key = None
        self._codec_frame_cache = None
        if path:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass

    def _start_playback(self):
        if not self.info or self._export_thread is not None:
            self._set_play_checked(False)
            return
        self._frame_timer.stop()
        self._stop_play_thread()

        codec_mode = is_codec_mode(str(self.mode.currentData() or ""))
        source = self.info
        if codec_mode and self._proxy_is_valid():
            source = self._proxy_info

        start = self.timeline.value() / 1000.0
        if start >= source.duration - 0.05:
            start = 0.0
            self._updating_slider = True
            self.timeline.setValue(0)
            self._updating_slider = False

        thread = PreviewPlayer(
            source, start, self._effects_snapshot(), self._seed,
            max_long=PLAY_LONG, fps=PLAY_FPS,
        )
        thread.frame_ready.connect(self._on_play_frame)
        thread.finished_ok.connect(self._on_play_finished)
        thread.failed.connect(self._on_play_failed)
        self._play_thread = thread
        thread.start()
        if self._audio_player is not None and self.info.has_audio and os.path.splitext(self.info.path)[1].lower() != ".gif":
            self._audio_player.setPosition(int(start * 1000))
            self._audio_player.play()

        self.play_btn.setText("⏸")
        self.state.setText("ВОСПРОИЗВЕДЕНИЕ")
        if codec_mode:
            self.footer.setText("Воспроизведение через реальный кодек FFmpeg (без звука).")
        else:
            self.footer.setText("Воспроизведение предпросмотра (без звука).")

    def toggle_play(self, checked: bool):
        if not checked:
            if self._proxy_thread is not None:
                self._pending_play = False
                self.footer.setText("Отмена подготовки прокси…")
                self._proxy_thread.cancel()
                return
            self.stop_playback()
            self.render_current_frame()
            return
        if not self.info or self._export_thread is not None:
            self._set_play_checked(False)
            return
        if self._proxy_thread is not None:
            return

        self._frame_timer.stop()
        self._stop_play_thread()

        mode = str(self.mode.currentData() or "")
        if is_codec_mode(mode) and not self._proxy_is_valid():
            self._start_proxy_build()
            return
        self._start_playback()

    def _on_play_frame(self, image: QImage, t: float):
        if self.sender() is not self._play_thread:
            return
        self._updating_slider = True
        self.timeline.setValue(int(max(0.0, t) * 1000))
        self._updating_slider = False
        self._update_time_label(t)
        self.preview.set_image(QPixmap.fromImage(image))
        self.state.setText("ВОСПРОИЗВЕДЕНИЕ")

    def _on_play_finished(self):
        thread = self._play_thread
        if thread is None or self.sender() is not thread:
            return
        self._play_thread = None
        if self._audio_player is not None:
            self._audio_player.stop()
        self._set_play_checked(False)
        self.play_btn.setText("▶")
        thread.deleteLater()
        self.render_current_frame()

    def _on_play_failed(self, message: str):
        thread = self._play_thread
        if thread is None or self.sender() is not thread:
            return
        self._play_thread = None
        self._set_play_checked(False)
        self.play_btn.setText("▶")
        thread.deleteLater()
        self.state.setText("ОШИБКА")
        self.footer.setText(f"Ошибка воспроизведения: {message}")

    def _stop_play_thread(self):
        thread = self._play_thread
        self._play_thread = None
        if self._audio_player is not None:
            self._audio_player.stop()
        if not thread:
            return
        for signal in (thread.frame_ready, thread.finished_ok, thread.failed):
            try:
                signal.disconnect()
            except Exception:
                pass
        thread.stop()
        if not thread.wait(5000):
            try:
                thread.terminate()
                thread.wait(1000)
            except Exception:
                pass
        thread.deleteLater()

    def stop_playback(self):
        self._stop_play_thread()
        self._set_play_checked(False)
        self.play_btn.setText("▶")

                                                                           
    def _set_export_busy(self, busy: bool):
        self.settings_scroll.setEnabled(not busy)
        self.open_btn.setEnabled(not busy)
        has_video = self.info is not None
        self.export_btn.setEnabled(not busy and has_video)
        self.play_btn.setEnabled(not busy and has_video)
        self.timeline.setEnabled(not busy and has_video)
        self.compare.setEnabled(not busy and has_video)
        self.reset_btn.setEnabled(not busy)
        self.cancel_btn.setVisible(busy)
        self.progress.setVisible(busy)

    def export_video(self):
        if self._export_thread is not None:
            return
        if not self.info:
            QMessageBox.information(self, "Нет видео", "Сначала откройте видеофайл.")
            return
        if not self._require_ffmpeg():
            return
        self.stop_playback()

        start = self.start_spin.value()
        end = self.end_spin.value()
        duration = end - start
        if duration < 0.05:
            QMessageBox.critical(self, "Неверная обрезка", "Конец должен быть позже начала.")
            return

        fmt = self.format.currentData() or "mp4"
        extensions = {"mp4": "mp4", "avi": "avi", "mov": "mov", "webm": "webm", "gif": "gif"}
        filters = {
            "mp4": "MP4 (*.mp4)", "avi": "AVI (*.avi)", "mov": "MOV (*.mov)",
            "webm": "WebM (*.webm)", "gif": "GIF (*.gif)",
        }
        ext = extensions[fmt]
        default = os.path.splitext(self.info.path)[0] + "_shakalized." + ext
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспортировать видео", default, filters[fmt] + ";;Все файлы (*)"
        )
        if not path:
            return
        root, current_ext = os.path.splitext(path)
        if current_ext.lower() != "." + ext:
            path = root + "." + ext
        if os.path.abspath(path) == os.path.abspath(self.info.path):
            QMessageBox.critical(
                self, "Нельзя перезаписать исходник",
                "Выберите другое имя файла: оригинал не изменяется.",
            )
            return

        size = self._target_size()
        if not size:
            return
        width, height = size
        fps_float, fps_arg = self._target_fps()
        settings = self._effects_snapshot()
        settings.update({
            "format": fmt,
            "quality": self.quality.value(),
            "audio": self.audio.isChecked(),
            "audio_shakal": self.audio_shakal.isChecked(),
            "audio_level": self.audio_level.value(),
            "gif_loop": self.gif_loop.isChecked(),
            "start": start,
            "duration": duration,
        })

        self._last_pct = -1
        thread = VideoExporter(
            self.info, settings, width, height, fps_float, fps_arg,
            path, self._seed,
        )
        thread.progress.connect(self._on_export_progress)
        thread.status.connect(self._on_export_status)
        thread.finished_ok.connect(self._on_export_finished)
        thread.failed.connect(self._on_export_failed)
        thread.cancelled.connect(self._on_export_cancelled)
        self._export_thread = thread
        self._set_export_busy(True)
        self.progress.setValue(0)
        self.state.setText("ЭКСПОРТ 0%")
        self.footer.setText(f"Экспорт: {width} × {height}, {fps_float:g} fps…")
        thread.start()

    def _on_export_progress(self, pct: int):
        if self.sender() is not self._export_thread:
            return
        self.progress.setValue(pct)
        self.state.setText(f"ЭКСПОРТ {pct}%")
        self.footer.setText(f"Экспорт: {pct}%")

    def _on_export_status(self, message: str):
        if self.sender() is not self._export_thread:
            return
        self.footer.setText(message)

    def _on_export_finished(self, path: str):
        thread = self._export_thread
        if thread is None or self.sender() is not thread:
            return
        self._export_thread = None
        thread.deleteLater()
        self._set_export_busy(False)
        self.state.setText("ЭКСПОРТ ГОТОВ")
        try:
            megabytes = os.path.getsize(path) / (1024 * 1024)
            self.footer.setText(f"Готово: {path} ({megabytes:.1f} МБ)")
        except Exception:
            self.footer.setText(f"Готово: {path}")
        try:
            if self._video_export_success is not None:
                self._video_export_success.play()
        except Exception:
            pass

        box = QMessageBox(self)
        box.setWindowTitle("Готово")
        box.setText(f"Видео сохранено:\n{path}")
        folder_btn = box.addButton("Открыть папку", QMessageBox.ActionRole)
        box.addButton("OK", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is folder_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))

    def _on_export_failed(self, message: str):
        thread = self._export_thread
        if thread is None or self.sender() is not thread:
            return
        self._export_thread = None
        thread.deleteLater()
        self._set_export_busy(False)
        self.state.setText("ОШИБКА")
        self.footer.setText("Экспорт не удался.")
        QMessageBox.critical(self, "Ошибка экспорта", message[:1600])

    def _on_export_cancelled(self):
        thread = self._export_thread
        if thread is None or self.sender() is not thread:
            return
        self._export_thread = None
        thread.deleteLater()
        self._set_export_busy(False)
        self.state.setText("ОТМЕНЕНО")
        self.footer.setText("Экспорт отменён, недописанный файл удалён.")

    def cancel_export(self):
        if self._proxy_thread is not None:
            self._pending_play = False
            self.footer.setText("Отмена подготовки прокси…")
            self._proxy_thread.cancel()
            return
        if self._export_thread is not None:
            self.footer.setText("Отмена экспорта…")
            self._export_thread.cancel()

                                                                          
    def reset_settings(self):
        if self._export_thread is not None or self._play_thread is not None:
            return
        self._muted_sliders = True
        self._suspend = True
        try:
            self.mode.setCurrentIndex(0)
            self.level.setValue(65)
            self.scale.setValue(100)
            self.blur.setValue(0)
            self.sharp.setValue(100)
            self.saturation.setValue(100)
            self.contrast.setValue(100)
            self.noise.setValue(0)
            self.grayscale.setChecked(False)
            self.custom_size.setChecked(False)
            index = self.fps_combo.findData("source")
            if index >= 0:
                self.fps_combo.setCurrentIndex(index)
            self.format.setCurrentIndex(0)
            self.quality.setValue(40)
            self.audio.setChecked(bool(self.info and self.info.has_audio))
            self.audio_shakal.setChecked(False)
            self.audio_level.setValue(70)
            self.gif_loop.setChecked(True)
            if self.info:
                self._set_spin(self.start_spin, 0.0)
                self._set_spin(self.end_spin, self.info.duration)
            self.compare.blockSignals(True)
            self.compare.setChecked(False)
            self.compare.blockSignals(False)
            self.compare.setText("Показать оригинал")
            self._show_original = False
        finally:
            self._suspend = False
            self._muted_sliders = False
        self._last_format = "mp4"
        self._update_format_ui()
        self._update_mode_hint()
        self._update_trim_label()
        self._update_export_info()
        self.render_current_frame()

                                                                             
    def on_tab_hidden(self):
        if self._proxy_thread is not None:
            self._invalidate_proxy()
        if self._play_thread is not None:
            self.stop_playback()
            self.render_current_frame()

    def shutdown(self):
        self._frame_timer.stop()
        self._invalidate_proxy()
        self._stop_play_thread()
        thread = self._export_thread
        self._export_thread = None
        if thread is not None:
            thread.cancel()
            try:
                thread.wait(6000)
            except Exception:
                pass








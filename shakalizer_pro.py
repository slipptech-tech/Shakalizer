"""
Shakalizer — modern PySide6 image and video degradation studio.
Install: py -m pip install -r requirements.txt
Run:     py shakalizer_pro.py
"""
import atexit
import io
import os
import shutil
import sys
import tempfile
import time
import wave
import numpy as np
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps
from PySide6.QtCore import Qt, QSize, QTimer, QEvent, QUrl
from PySide6.QtGui import QPixmap, QImage, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton, QFileDialog,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFrame, QSlider, QSpinBox,
    QCheckBox, QComboBox, QScrollArea, QMessageBox, QSizePolicy, QProgressBar,
    QAbstractButton, QTabWidget
)

from shakalizer_video import VIDEO_EXTS, VideoEditorPage

                                                                                                
try:
    from PySide6.QtMultimedia import QSoundEffect, QMediaDevices
except Exception:
    QSoundEffect = None
    QMediaDevices = None

APP_TITLE = "Shakalizer"
LOGO_FILE = "SPLogo.png"
LOGO_HEIGHT = 100                                                                                           

def lerp(a, b, t):
    return a + (b - a) * t


def jpeg_roundtrip(img, quality):
    """Сохраняет картинку в JPEG с заданным качеством и загружает обратно."""
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=int(max(1, min(95, quality))), subsampling=2)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def shrink_to(img, work_long):
    """Уменьшает картинку так, чтобы её длинная сторона стала work_long."""
    w, h = img.size
    work_long = max(8, min(max(w, h), int(work_long)))
    k = work_long / max(w, h)
    return img.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)


def classic(img, level):
    """Классическая: убитый JPEG, месиво из квадратиков."""
    t = level / 100
    w, h = img.size
    small = shrink_to(img, lerp(900, 150, t))
    quality = lerp(30, 1, t ** 0.6)
    passes = 1 + int(t * 4)

    for i in range(passes):
                                                                               
                                                                           
                                                      
        dx, dy = (i * 3) % 8, (i * 5) % 8
        shifted = ImageChops.offset(small, dx, dy)
        shifted = jpeg_roundtrip(shifted, quality)
        small = ImageChops.offset(shifted, -dx, -dy)

    small = ImageEnhance.Color(small).enhance(1 + 0.7 * t)
    return small.resize((w, h), Image.NEAREST)


def minecraft(img, level):
    """Майнкрафтизация: пиксели разных размеров (квадро-дерево со случайным делением)."""
    t = level / 100
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    H, W = arr.shape[:2]

    unit = max(1, round(max(W, H) * lerp(0.0006, 0.0065, t)))
    big = 8 * unit
    sizes = [big, big // 2, big // 4, big // 8]                              

    pad_h, pad_w = (-H) % big, (-W) % big
    arr = np.pad(arr, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
    Hp, Wp = arr.shape[:2]

    rng = np.random.default_rng()
    out = np.zeros_like(arr)
    decided = None

    for k, s in enumerate(sizes):
        gh, gw = Hp // s, Wp // s
                                          
        cells = np.asarray(
            Image.fromarray(arr).resize((gw, gh), Image.BOX), dtype=np.uint8
        )

        if decided is None:
            decided = np.zeros((gh, gw), dtype=bool)
        else:
            decided = np.repeat(np.repeat(decided, 2, axis=0), 2, axis=1)

        if k == len(sizes) - 1:
            stop = ~decided
        else:
            stop = (~decided) & (rng.random((gh, gw)) < 0.4)
        decided = decided | stop

        cell_img = np.repeat(np.repeat(cells, s, axis=0), s, axis=1)
        mask = np.repeat(np.repeat(stop, s, axis=0), s, axis=1)
        out[mask] = cell_img[mask]

    return Image.fromarray(out[:H, :W])


def ultramylo(img, level):
    """Ультрамыло: крошечное разрешение + JPEG + размытие."""
    t = level / 100
    w, h = img.size
    small = shrink_to(img, lerp(360, 36, t))
    sw, sh = small.size
    quality = lerp(25, 3, t)
    passes = 2 + int(t * 3)

    for _ in range(passes):
        small = jpeg_roundtrip(small, quality)
        small = (
            small.resize((sw * 2, sh * 2), Image.BICUBIC)
            .filter(ImageFilter.GaussianBlur(0.6 + t))
            .resize((sw, sh), Image.BILINEAR)
        )

    big = small.resize((w, h), Image.BICUBIC)
    blur_radius = max(0.5, (w / sw) * lerp(0.35, 0.6, t))
    big = big.filter(ImageFilter.GaussianBlur(blur_radius))
    big = ImageEnhance.Contrast(big).enhance(1 - 0.2 * t)
    return jpeg_roundtrip(big, lerp(40, 8, t))


MODES = {
    "classic": ("Классическая", classic),
    "minecraft": ("Майнкрафтизация", minecraft),
    "ultramylo": ("Ультрамыло", ultramylo),
}




def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ----------------------------------------------------------------------------
                   
# ----------------------------------------------------------------------------
                                                                                    
                                                                         
SOUND_FILES = {
    "hover":  ("buttonrollover.wav",     0.20, 2),
    "click":  ("buttonclickrelease.wav", 0.32, 3),
    "slider": ("slider.wav",             0.22, 3),
    "export": ("gift_drop.wav",          0.40, 1),
    "error":  ("error.wav",              0.35, 1),
}
MASTER_VOLUME = 0.8                              
HOVER_COOLDOWN_MS = 90                                                  
SLIDER_COOLDOWN_MS = 45                                                
FADE_IN_MS = 3                                                           
FADE_OUT_MS = 15                                                      
PAD_START_MS = 15                                                                                 
PAD_END_MS = 50                                                               


def read_wav_mono(path):
    """Читает PCM-WAV (8/16/24/32 бит, любые каналы) и возвращает (float-массив моно, частота)."""
    with wave.open(path, "rb") as w:
        ch, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(frames)
    if width == 1:
        data = (np.frombuffer(raw, np.uint8).astype(np.float64) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, "<i2").astype(np.float64) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v & 0x800000, v - 0x1000000, v)
        data = v.astype(np.float64) / 8388608.0
    elif width == 4:
        data = np.frombuffer(raw, "<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"Неподдерживаемая разрядность WAV: {width * 8} бит")
    if ch > 1:
        data = data[: len(data) // ch * ch].reshape(-1, ch).mean(axis=1)
    return data, rate


def fft_resample(x, rate_in, rate_out):
    """Пересэмплирование через FFT (без scipy). Края звука к этому моменту уже затухают в ноль."""
    if rate_in == rate_out or len(x) < 2:
        return x
    n = len(x)
    m = max(2, int(round(n * rate_out / rate_in)))
    spec = np.fft.rfft(x)
    out = np.zeros(m // 2 + 1, dtype=complex)
    k = min(len(spec), len(out))
    out[:k] = spec[:k]
    return np.fft.irfft(out, m) * (m / n)


def prepare_sound(src, dst, rate_out, peak):
    """Делает из исходного WAV «безопасный» для воспроизведения файл:
    убирает смещение, добавляет плавные края и тишину по краям, подгоняет под частоту
    звуковой карты и выставляет заданную пиковую громкость."""
    x, rate = read_wav_mono(src)
    x = x - x.mean()                                                                                       

    fade_in = min(int(rate * FADE_IN_MS / 1000), len(x) // 4)
    fade_out = min(int(rate * FADE_OUT_MS / 1000), len(x) // 4)
    if fade_in > 1:
        x[:fade_in] *= 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, fade_in))
    if fade_out > 1:
        x[-fade_out:] *= 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, fade_out))

    lead = np.zeros(int(rate * PAD_START_MS / 1000))
    tail = np.zeros(int(rate * PAD_END_MS / 1000))
    x = np.concatenate([lead, x, tail])

    x = fft_resample(x, rate, rate_out)
    top = np.abs(x).max()
    if top > 0:
        x = x * (min(max(peak, 0.01), 0.95) / top)                                                 

    pcm = np.round(x * 32767).astype("<i2")
    with wave.open(dst, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate_out))
        w.writeframes(pcm.tobytes())


class SoundManager:
    """Звуки кнопок и экспорта. Если что-то пошло не так — молча работает без звука."""

    def __init__(self, parent):
        self.enabled = False
        self._pools = {}
        self._last_hover = 0.0
        self._last_slider = 0.0
        if QSoundEffect is None:
            return
        try:
            rate = 48000
            if QMediaDevices is not None:
                device_rate = QMediaDevices.defaultAudioOutput().preferredFormat().sampleRate()
                if device_rate >= 8000:
                    rate = int(device_rate)                                                  
            self._dir = tempfile.mkdtemp(prefix="shakalizer_sounds_")
            atexit.register(shutil.rmtree, self._dir, True)
            for name, (filename, peak, voices) in SOUND_FILES.items():
                src = resource_path(filename)
                if not os.path.isfile(src):
                    continue
                dst = os.path.join(self._dir, name + ".wav")
                prepare_sound(src, dst, rate, peak)
                pool = []
                for _ in range(voices):
                    fx = QSoundEffect(parent)
                    fx.setSource(QUrl.fromLocalFile(dst))
                    fx.setVolume(MASTER_VOLUME)
                    pool.append(fx)
                self._pools[name] = pool
            self.enabled = bool(self._pools)
        except Exception as e:
            print(f"[sound] звук отключён: {e}")

    def play(self, name):
                                                                                             
        for fx in self._pools.get(name, ()):
            if not fx.isPlaying():
                fx.play()
                return

    def play_hover(self):
        now = time.monotonic()
        if now - self._last_hover >= HOVER_COOLDOWN_MS / 1000:
            self._last_hover = now
            self.play("hover")

    def play_slider(self):
        now = time.monotonic()
        if now - self._last_slider >= SLIDER_COOLDOWN_MS / 1000:
            self._last_slider = now
            self.play("slider")


def jpeg_roundtrip(img, quality):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=max(1, min(95, int(quality))), optimize=False)
    buf.seek(0)
    with Image.open(buf) as out:
        return out.convert("RGB").copy()


class PreviewLabel(QLabel):
    def __init__(self):
        super().__init__("Откройте изображение или перетащите его сюда")
        self.setObjectName("preview")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(380, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAcceptDrops(True)
        self._pix = None

    def set_image(self, pix):
        self._pix = pix
        self._scale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scale()

    def _scale(self):
        if self._pix and not self._pix.isNull():
            self.setPixmap(self._pix.scaled(self.size() - QSize(24, 24),
                                            Qt.KeepAspectRatio, Qt.SmoothTransformation))

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
                    window.load_image(path)
                event.acceptProposedAction()
                return


class Panel(QFrame):
    def __init__(self, title, hint=""):
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


class ShakalizerApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Shakalizer")
        self.resize(1320, 900)
        self.setMinimumSize(1080, 720)
        self.original = None
        self.source_path = None
        self.result = None
        self._showing_original = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self.update_preview)
        self._sliders_muted = False
        self.sounds = SoundManager(self)
        self._build_shell()
        self._style()
        self._wire_button_sounds()

    def _build_shell(self):
        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 10, 20, 14)
        root.setSpacing(8)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.tabBar().installEventFilter(self)
        self._hovered_category = -1

        self.image_page = QWidget()
        self.image_page.setObjectName("page")
        self._build_ui(self.image_page)
        self.tabs.addTab(self.image_page, "Фото")

        self.video_page = VideoEditorPage(self, self.sounds, self._load_logo_pixmap())
        self.tabs.addTab(self.video_page, "Видео")
        self.tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(self.tabs, 1)

    def _tab_changed(self, index):
        if index != 1:
            self.video_page.on_tab_hidden()

    def load_media(self, path):
        """Открывает файл в нужной вкладке: картинка — «Фото», видео — «Видео»."""
        extension = os.path.splitext(path)[1].lower()
        if extension in VIDEO_EXTS:
            self.tabs.setCurrentWidget(self.video_page)
            self.video_page.load_video(path)
        else:
            self.tabs.setCurrentWidget(self.image_page)
            self.load_image(path)

    def closeEvent(self, event):
        try:
            self.video_page.shutdown()
        except Exception:
            pass
        super().closeEvent(event)

    @staticmethod
    def _load_logo_pixmap():
        """Загружает логотип и обрезает пустые прозрачные поля, чтобы он не казался мелким."""
        path = resource_path(LOGO_FILE)
        if not os.path.isfile(path):
            return None
        try:
            im = Image.open(path).convert("RGBA")
            box = im.getchannel("A").point(lambda v: 255 if v > 10 else 0).getbbox()
            if box:
                im = im.crop(box)
            qimg = QImage(im.tobytes("raw", "RGBA"), im.width, im.height,
                          im.width * 4, QImage.Format_RGBA8888).copy()
            return QPixmap.fromImage(qimg)
        except Exception:
            return QPixmap(path)

    def _make_logo(self):
        """Логотип в левом верхнем углу. Если файла нет — показываем обычный текст."""
        label = QLabel()
        label.setObjectName("logo")
        pix = self._load_logo_pixmap()
        if pix is None or pix.isNull():
            label.setText("SHAKALIZER PRO")
            label.setObjectName("brand")
            return label
        dpr = self.devicePixelRatioF()                                                  
        scaled = pix.scaledToHeight(int(LOGO_HEIGHT * dpr), Qt.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        label.setPixmap(scaled)
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        return label

    def _slider_sound(self, *_):
        if not self._sliders_muted:
            self.sounds.play_slider()

    def _wire_button_sounds(self):
        for btn in self.findChildren(QAbstractButton):                      
            btn.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.tabs.tabBar():
            bar = self.tabs.tabBar()
            kind = event.type()
            if kind == QEvent.MouseMove:
                index = bar.tabAt(event.position().toPoint())
                if index >= 0 and index != self._hovered_category:
                    self._hovered_category = index
                    self.sounds.play_hover()
                elif index < 0:
                    self._hovered_category = -1
            elif kind == QEvent.Leave:
                self._hovered_category = -1
            elif kind == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                if bar.tabAt(event.position().toPoint()) >= 0:
                    self.sounds.play("click")
        if isinstance(obj, QAbstractButton) and obj.isEnabled():
            kind = event.type()
            if kind == QEvent.Enter:
                self.sounds.play_hover()
            elif (kind == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton
                  and obj.rect().contains(event.position().toPoint())):
                                                                                           
                                                                                         
                self.sounds.play("click")
        return super().eventFilter(obj, event)

    def _build_ui(self, central):
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 10, 0, 0)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.addWidget(self._make_logo())
        header.addStretch()
        self.open_btn = QPushButton("Открыть изображение")
        self.open_btn.setObjectName("primary")
        self.open_btn.clicked.connect(self.open_image)
        header.addWidget(self.open_btn)
        self.export_btn = QPushButton("Экспортировать")
        self.export_btn.setObjectName("primary")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export_image)
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
        t = QLabel("Предпросмотр результата")
        t.setObjectName("panelTitle")
        pvhead.addWidget(t)
        pvhead.addStretch()
        self.state = QLabel("ОЖИДАНИЕ")
        self.state.setObjectName("pill")
        pvhead.addWidget(self.state)
        pv.addLayout(pvhead)
        self.preview = PreviewLabel()
        pv.addWidget(self.preview, 1)
        inf = QHBoxLayout()
        self.dim_label = QLabel("Размер: —")
        self.dim_label.setObjectName("muted")
        self.file_label = QLabel("Файл не выбран")
        self.file_label.setObjectName("muted")
        inf.addWidget(self.dim_label)
        inf.addStretch()
        inf.addWidget(self.file_label)
        pv.addLayout(inf)
        left.addWidget(preview_panel, 1)
        actions = QHBoxLayout()
        self.compare = QPushButton("Показать оригинал")
        self.compare.setCheckable(True)
        self.compare.toggled.connect(self.toggle_original)
        actions.addWidget(self.compare)
        reset = QPushButton("Сбросить параметры")
        reset.clicked.connect(self.reset_settings)
        actions.addWidget(reset)
        actions.addStretch()
        left.addLayout(actions)
        body.addLayout(left, 7)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumWidth(330)
        scroll.setMaximumWidth(410)
        host = QWidget()
        host.setObjectName("settingsHost")
        side = QVBoxLayout(host)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(11)

        mode_panel = Panel("Режим шакализации", "Выберите алгоритм и силу эффекта.")
        self.mode = QComboBox()
        self.mode.addItem("Классическая JPEG-деградация", "classic")
        self.mode.addItem("Minecraft-пикселизация", "minecraft")
        self.mode.addItem("Ультрамыло", "ultramylo")
        self.mode.currentIndexChanged.connect(self.schedule_preview)
        mode_panel.box.addWidget(self.mode)
        self.mode_strength, self.mode_strength_label = self.add_slider(mode_panel, "Интенсивность", 1, 100, 65, "%")
        side.addWidget(mode_panel)

        size_panel = Panel("Размер изображения", "Масштаб влияет на детализацию и скорость обработки.")
        self.scale, self.scale_label = self.add_slider(size_panel, "Масштаб", 5, 100, 45, "%")
        self.scale.valueChanged.connect(self.schedule_preview)
        self.custom_size = QCheckBox("Задать размер вручную")
        self.custom_size.toggled.connect(self.toggle_custom_size)
        size_panel.box.addWidget(self.custom_size)
        dims = QGridLayout()
        dims.addWidget(QLabel("Ширина"), 0, 0)
        self.width_spin = QSpinBox()
        self.width_spin.setRange(16, 16000)
        self.width_spin.setValue(1280)
        self.width_spin.setEnabled(False)
        dims.addWidget(self.width_spin, 0, 1)
        dims.addWidget(QLabel("Высота"), 1, 0)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(16, 16000)
        self.height_spin.setValue(720)
        self.height_spin.setEnabled(False)
        dims.addWidget(self.height_spin, 1, 1)
        size_panel.box.addLayout(dims)
        self.keep_ratio = QCheckBox("Сохранять пропорции")
        self.keep_ratio.setChecked(True)
        size_panel.box.addWidget(self.keep_ratio)
        side.addWidget(size_panel)

        effects = Panel("Дополнительные эффекты", "Эффекты применяются после основного алгоритма.")
        self.blur, self.blur_label = self.add_slider(effects, "Размытие", 0, 10, 0, "")
        self.sharp, self.sharp_label = self.add_slider(effects, "Резкость", 0, 250, 100, "%")
        self.saturation, self.saturation_label = self.add_slider(effects, "Насыщенность", 0, 200, 100, "%")
        self.contrast, self.contrast_label = self.add_slider(effects, "Контраст", 50, 200, 100, "%")
        self.noise, self.noise_label = self.add_slider(effects, "Цифровой шум", 0, 60, 0, "%")
        self.grayscale = QCheckBox("Чёрно-белый режим")
        effects.box.addWidget(self.grayscale)
        for control in (self.blur, self.sharp, self.saturation, self.contrast, self.noise):
            control.valueChanged.connect(self.schedule_preview)
        self.grayscale.stateChanged.connect(self.schedule_preview)
        side.addWidget(effects)

        export = Panel("Параметры экспорта", "Оригинальный файл не изменяется.")
        row = QHBoxLayout()
        row.addWidget(QLabel("Формат"))
        self.format = QComboBox()
        self.format.addItems(["JPEG", "PNG", "WEBP"])
        row.addWidget(self.format)
        export.box.addLayout(row)
        self.quality, self.quality_label = self.add_slider(export, "Качество JPEG / WebP", 1, 100, 35, "%")
        self.quality.valueChanged.connect(self.schedule_preview)
        self.format.currentTextChanged.connect(self._format_changed)
        self.metadata = QCheckBox("Сохранять EXIF-метаданные")
        export.box.addWidget(self.metadata)
        side.addWidget(export)
        side.addStretch()
        scroll.setWidget(host)
        body.addWidget(scroll, 3)

        self.progress = QProgressBar()
        self.progress.setFixedHeight(3)
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.progress.hide()
        root.addWidget(self.progress)
        self.footer = QLabel("Готово. Обработка выполняется локально.")
        self.footer.setObjectName("muted")
        root.addWidget(self.footer)

    def add_slider(self, panel, title, lo, hi, val, suffix):
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        top = QHBoxLayout()
        name = QLabel(title)
        name.setObjectName("controlLabel")
        value = QLabel(f"{val}{suffix}")
        value.setObjectName("value")
        top.addWidget(name)
        top.addStretch()
        top.addWidget(value)
        lay.addLayout(top)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(val)
        slider.valueChanged.connect(lambda v, lab=value, suf=suffix: lab.setText(f"{v}{suf}"))
        slider.valueChanged.connect(self.schedule_preview)
        slider.valueChanged.connect(self._slider_sound)
        lay.addWidget(slider)
        panel.box.addWidget(wrap)
        return slider, value

    def _style(self):
        self.setStyleSheet("""
        QWidget#root, QWidget#page, QWidget#settingsHost { background:#0b0f15; color:#e8edf6; font-family:"Segoe UI"; font-size:13px; }
        QTabWidget#mainTabs::pane { border:none; background:transparent; }
        QTabWidget#mainTabs::tab-bar { alignment:left; }
        QTabBar { background:transparent; }
        QTabBar::tab { background:#121822; color:#8793a8; border:1px solid #242e3d; border-radius:9px; padding:7px 20px; margin-right:6px; font-weight:700; font-size:12px; }
        QTabBar::tab:hover:!selected { background:#1a2230; color:#dce5f4; border-color:#536c96; }
        QTabBar::tab:selected { background:#5278f6; color:#ffffff; border-color:#6789ff; }
        QLabel#brand { font-size:25px; font-weight:800; letter-spacing:2px; color:#f7f9ff; }
        QLabel#muted { color:#8793a8; }
        QLabel#panelTitle { font-size:14px; font-weight:700; color:#f0f4fc; }
        QLabel#controlLabel { color:#c7d0df; }
        QLabel#value { color:#8faaff; font-weight:700; }
        QLabel#pill { background:#182746; color:#9bb7ff; border:1px solid #2b416d; border-radius:9px; padding:5px 9px; font-size:10px; font-weight:700; }
        QFrame#panel, QFrame#previewPanel { background:#121822; border:1px solid #242e3d; border-radius:13px; }
        QLabel#preview { background:#0a0d12; color:#68768b; border:1px dashed #303c50; border-radius:10px; padding:10px; font-size:15px; }
        QPushButton { background:#1a2230; color:#dce5f4; border:1px solid #2a374a; border-radius:8px; padding:10px 13px; font-weight:600; }
        QPushButton:hover { background:#253247; border-color:#536c96; }
        QPushButton:disabled { color:#596578; background:#141922; }
        QPushButton#primary { background:#5278f6; color:white; border:1px solid #6789ff; }
        QPushButton#primary:hover { background:#6487ff; }
        QPushButton#transport { background:#1a2230; color:#dce5f4; border:1px solid #2a374a; border-radius:8px; padding:5px; font-size:17px; font-weight:700; }
        QPushButton#transport:hover { background:#253247; border-color:#536c96; }
        QPushButton#transport:checked { background:#5278f6; border-color:#6789ff; color:white; }
        QComboBox, QSpinBox, QDoubleSpinBox { background:#0d131c; color:#e8edf6; border:1px solid #2a374a; border-radius:7px; padding:7px; min-width:80px; }
        QComboBox QAbstractItemView { background:#151c27; color:#e8edf6; selection-background-color:#304987; }
        QCheckBox { color:#cbd4e2; spacing:8px; }
        QCheckBox::indicator { width:16px; height:16px; border:1px solid #3a4960; border-radius:4px; background:#0d131c; }
        QCheckBox::indicator:checked { background:#6485ff; border-color:#6485ff; }
        QSlider::groove:horizontal { height:5px; background:#293447; border-radius:2px; }
        QSlider::sub-page:horizontal { background:#6485ff; border-radius:2px; }
        QSlider::handle:horizontal { background:#f1f5ff; width:13px; margin:-5px 0; border-radius:6px; border:2px solid #6485ff; }
        QScrollArea { border:none; background:transparent; }
        QProgressBar { border:none; background:#0b0f15; }
        QProgressBar::chunk { background:#6485ff; }
        """)

    def open_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Открыть изображение", "",
            "Изображения (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff);;Все файлы (*)")
        if path:
            self.load_image(path)

    def load_image(self, path):
        try:
            with Image.open(path) as im:
                self.original = ImageOps.exif_transpose(im).convert("RGB").copy()
            self.source_path = path
            self.width_spin.setValue(self.original.width)
            self.height_spin.setValue(self.original.height)
            self.export_btn.setEnabled(True)
            self.compare.blockSignals(True)
            self.compare.setChecked(False)
            self.compare.blockSignals(False)
            self.state.setText("ИЗОБРАЖЕНИЕ ЗАГРУЖЕНО")
            self.file_label.setText(os.path.basename(path))
            self.footer.setText(f"Загружено: {os.path.basename(path)}")
            self.schedule_preview()
        except Exception as e:
            QMessageBox.critical(self, "Не удалось открыть файл", str(e))

    def toggle_custom_size(self, checked):
        self.width_spin.setEnabled(checked)
        self.height_spin.setEnabled(checked)
        self.schedule_preview()

    def _format_changed(self, value):
        self.quality.setEnabled(value in ("JPEG", "WEBP"))
        self.schedule_preview()

    def schedule_preview(self, *_):
        if hasattr(self, "_timer"):
            self._timer.start()

    def toggle_original(self, checked):
        if self.original is None:
                                                                                              
            self.compare.blockSignals(True)
            self.compare.setChecked(False)
            self.compare.blockSignals(False)
            self.sounds.play("error")
            QMessageBox.critical(self, "Ошибка", "Сначала загрузите картинку")
            return
        self._showing_original = checked
        self.compare.setText("Показать результат" if checked else "Показать оригинал")
        if checked:
            self._show_image(self.original.copy())
        else:
            self.update_preview()

    def _target_size(self, img, preview=False):
        if self.custom_size.isChecked():
            w, h = self.width_spin.value(), self.height_spin.value()
            if self.keep_ratio.isChecked():
                ratio = min(w / img.width, h / img.height)
                w, h = max(1, round(img.width * ratio)), max(1, round(img.height * ratio))
        else:
            scale = self.scale.value() / 100
            w, h = max(1, round(img.width * scale)), max(1, round(img.height * scale))
        if preview:
            factor = min(1.0, 900 / max(w, h))
            w, h = max(1, round(w * factor)), max(1, round(h * factor))
        return w, h

    def process_image(self, source, preview=False):
        img = source.copy().convert("RGB")
        target = self._target_size(img, preview)
        if img.size != target:
            img = img.resize(target, Image.Resampling.LANCZOS)
        mode = self.mode.currentData()
        level = self.mode_strength.value()
        if mode == "classic":
            img = classic(img, level)
        elif mode == "minecraft":
            img = minecraft(img, level)
        else:
            img = ultramylo(img, level)
        if self.blur.value():
            img = img.filter(ImageFilter.GaussianBlur(self.blur.value() / 2))
        if self.sharp.value() != 100:
            img = ImageEnhance.Sharpness(img).enhance(self.sharp.value() / 100)
        if self.saturation.value() != 100:
            img = ImageEnhance.Color(img).enhance(self.saturation.value() / 100)
        if self.contrast.value() != 100:
            img = ImageEnhance.Contrast(img).enhance(self.contrast.value() / 100)
        if self.grayscale.isChecked():
            img = ImageOps.grayscale(img).convert("RGB")
        if self.noise.value():
            arr = np.asarray(img).astype(np.int16)
            rng = np.random.default_rng()
            sigma = self.noise.value() * 0.55
            arr = np.clip(arr + rng.normal(0, sigma, arr.shape), 0, 255).astype(np.uint8)
            img = Image.fromarray(arr, "RGB")
        return img

    def update_preview(self):
        if self.original is None:
            return
        if self._showing_original:
            self._show_image(self.original)
            return
        try:
            self.result = self.process_image(self.original, preview=True)
            self._show_image(self.result)
            self.state.setText("ПРЕДПРОСМОТР ГОТОВ")
            self.dim_label.setText(f"Размер результата: {self.result.width} × {self.result.height}")
        except Exception as e:
            self.footer.setText(f"Ошибка предпросмотра: {e}")

    def _show_image(self, img):
        rgba = img.convert("RGBA")
        raw = rgba.tobytes("raw", "RGBA")
        qimg = QImage(raw, rgba.width, rgba.height, rgba.width * 4, QImage.Format_RGBA8888).copy()
        self.preview.set_image(QPixmap.fromImage(qimg))

    def reset_settings(self):
        self._sliders_muted = True                                                                 
        try:
            self._reset_settings()
        finally:
            self._sliders_muted = False
        self.schedule_preview()

    def _reset_settings(self):
        self.mode.setCurrentIndex(0)
        self.mode_strength.setValue(65)
        self.scale.setValue(45)
        self.blur.setValue(0)
        self.sharp.setValue(100)
        self.saturation.setValue(100)
        self.contrast.setValue(100)
        self.noise.setValue(0)
        self.grayscale.setChecked(False)
        self.custom_size.setChecked(False)
        self.format.setCurrentText("JPEG")
        self.quality.setValue(35)

    def export_image(self):
        if self.original is None:
            return
        fmt = self.format.currentText()
        extensions = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
        default = os.path.splitext(self.source_path or "shakalized")[0] + "_shakalized." + extensions[fmt]
        path, _ = QFileDialog.getSaveFileName(self, "Экспортировать изображение", default,
            "JPEG (*.jpg *.jpeg);;PNG (*.png);;WebP (*.webp)")
        if not path:
            return
        self.progress.show()
        self.export_btn.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            # Export full resolution; preview alone is downscaled for responsiveness.
            result = self.process_image(self.original, preview=False)
            save_fmt = fmt
            ext = os.path.splitext(path)[1].lower()
            if ext in (".jpg", ".jpeg"):
                save_fmt = "JPEG"
            elif ext == ".png":
                save_fmt = "PNG"
            elif ext == ".webp":
                save_fmt = "WEBP"
            options = {}
            if save_fmt in ("JPEG", "WEBP"):
                options["quality"] = self.quality.value()
            if save_fmt == "JPEG":
                options["optimize"] = True
            if self.metadata.isChecked() and self.source_path:
                try:
                    with Image.open(self.source_path) as original_file:
                        exif = original_file.info.get("exif")
                    if exif:
                        options["exif"] = exif
                except Exception:
                    pass
            if save_fmt == "JPEG":
                result.convert("RGB").save(path, "JPEG", **options)
            else:
                result.save(path, save_fmt, **options)
            self.result = result
            self.footer.setText(f"Экспортировано: {path}")
            self.state.setText("ЭКСПОРТ ГОТОВ")
            self.sounds.play("export")
            QMessageBox.information(self, "Готово", f"Изображение сохранено:\n{path}")
            if not self._showing_original:
                self._show_image(result)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка экспорта", str(e))
        finally:
            self.progress.hide()
            self.export_btn.setEnabled(True)
            QApplication.restoreOverrideCursor()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    window = ShakalizerApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

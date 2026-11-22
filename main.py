"""
Reddit Video Maker - Main Application
A PySide6 GUI application for creating YouTube videos from Reddit stories.
"""

import sys
import os
import tempfile
import shutil

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QLineEdit, QPushButton, QTextEdit, QComboBox,
    QSpinBox, QDoubleSpinBox, QSlider, QFileDialog, QProgressBar,
    QListWidget, QListWidgetItem, QGroupBox, QFormLayout, QCheckBox,
    QMessageBox, QStatusBar, QSplitter, QFrame, QScrollArea, QGridLayout,
    QDialog, QColorDialog, QRadioButton, QButtonGroup, QSizePolicy,
    QStyleFactory
)
from PySide6.QtCore import (
    Qt, QThread, Signal, QObject, QTimer, QSize, QSettings, QUrl, QMutex
)
from PySide6.QtGui import (
    QFont, QIcon, QPixmap, QPalette, QColor, QDesktopServices, QIntValidator
)

# Try to import multimedia for in-app preview
try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAS_MULTIMEDIA = True
except ImportError:
    HAS_MULTIMEDIA = False

from reddit_fetcher import RedditFetcher, RedditStory
from tts_engine import TTSEngine, WordBoundary
from video_composer import VideoComposer


# ============================================================================
# Worker Threads
# ============================================================================

class FetchStoriesWorker(QThread):
    """Worker thread for fetching Reddit stories."""
    stories_ready = Signal(list)
    error = Signal(str)

    def __init__(self, fetcher, subreddit, sort, limit, time_filter):
        super().__init__()
        self.fetcher = fetcher
        self.subreddit = subreddit
        self.sort = sort
        self.limit = limit
        self.time_filter = time_filter

    def run(self):
        try:
            stories = self.fetcher.fetch_stories(
                self.subreddit, self.sort, self.limit, self.time_filter
            )
            self.stories_ready.emit(stories)
        except Exception as e:
            self.error.emit(str(e))


class FetchCommentsWorker(QThread):
    """Worker thread for fetching comments."""
    comments_ready = Signal(list)
    error = Signal(str)

    def __init__(self, fetcher, subreddit, story_id):
        super().__init__()
        self.fetcher = fetcher
        self.subreddit = subreddit
        self.story_id = story_id

    def run(self):
        try:
            comments = self.fetcher.fetch_comments(
                self.subreddit, self.story_id
            )
            self.comments_ready.emit(comments)
        except Exception as e:
            self.error.emit(str(e))


class TTSWorker(QThread):
    """Worker thread for TTS generation."""
    audio_ready = Signal(str, list)
    error = Signal(str)

    def __init__(self, engine, text, voice, output_path, rate, volume):
        super().__init__()
        self.engine = engine
        self.text = text
        self.voice = voice
        self.output_path = output_path
        self.rate = rate
        self.volume = volume

    def run(self):
        try:
            audio_path, boundaries = self.engine.generate(
                self.text, self.voice, self.output_path,
                self.rate, self.volume
            )
            self.audio_ready.emit(audio_path, boundaries)
        except Exception as e:
            self.error.emit(str(e))


class TTSPreviewWorker(QThread):
    """Worker thread for TTS preview."""
    preview_ready = Signal(str)
    error = Signal(str)

    def __init__(self, engine, text, voice, output_path):
        super().__init__()
        self.engine = engine
        self.text = text
        self.voice = voice
        self.output_path = output_path

    def run(self):
        try:
            self.engine.preview(self.text, self.voice, self.output_path)
            self.preview_ready.emit(self.output_path)
        except Exception as e:
            self.error.emit(str(e))


class VideoComposeWorker(QThread):
    """Worker thread for video composition."""
    video_ready = Signal(str)
    progress = Signal(int, str)
    error = Signal(str)

    def __init__(self, composer, params):
        super().__init__()
        self.composer = composer
        self.params = params

    def run(self):
        try:
            output = self.composer.compose(
                progress_callback=lambda pct, msg: self.progress.emit(pct, msg),
                **self.params
            )
            self.video_ready.emit(output)
        except Exception as e:
            self.error.emit(str(e))


# ============================================================================
# Settings Dialog
# ============================================================================

class SettingsDialog(QDialog):
    """Application settings dialog."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(450)

        layout = QVBoxLayout(self)

        # Output directory
        out_group = QGroupBox("Default Output Directory")
        out_layout = QHBoxLayout(out_group)
        self.output_dir_edit = QLineEdit(
            self.settings.value('paths/output_dir', os.path.expanduser('~'))
        )
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_output_dir)
        out_layout.addWidget(self.output_dir_edit)
        out_layout.addWidget(browse_btn)
        layout.addWidget(out_group)

        # FFmpeg
        ffmpeg_group = QGroupBox("FFmpeg")
        ffmpeg_layout = QVBoxLayout(ffmpeg_group)
        ffmpeg_path = shutil.which('ffmpeg') or 'Not found in PATH'
        if os.environ.get('FFMPEG_BINARY'):
            ffmpeg_path = os.environ['FFMPEG_BINARY']

        ffmpeg_label = QLabel(f"FFmpeg location: {ffmpeg_path}")
        ffmpeg_label.setWordWrap(True)
        ffmpeg_layout.addWidget(ffmpeg_label)

        info_label = QLabel(
            "If FFmpeg is not found, install it or the 'imageio-ffmpeg' "
            "Python package will provide a bundled version."
        )
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #aaa; font-size: 11px;")
        ffmpeg_layout.addWidget(info_label)
        layout.addWidget(ffmpeg_group)

        # Buttons
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def _browse_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select Output Directory")
        if d:
            self.output_dir_edit.setText(d)

    def save_settings(self):
        self.settings.setValue('paths/output_dir', self.output_dir_edit.text())


# ============================================================================
# Main Window
# ============================================================================

class MainWindow(QMainWindow):
    """Main application window."""

    # Dark theme stylesheet
    DARK_THEME = """
        QMainWindow, QDialog { background-color: #1a1a2e; }
        QTabWidget::pane {
            background-color: #16213e;
            border: 1px solid #0f3460;
            border-radius: 5px;
            top: -1px;
        }
        QTabBar::tab {
            background-color: #0f3460;
            color: #c0c0d0;
            padding: 10px 24px;
            border-radius: 5px 5px 0 0;
            margin-right: 2px;
            font-weight: bold;
        }
        QTabBar::tab:selected {
            background-color: #e94560;
            color: white;
        }
        QTabBar::tab:hover:!selected {
            background-color: #1a4a7a;
        }
        QPushButton {
            background-color: #0f3460;
            color: #e0e0f0;
            border: none;
            padding: 8px 18px;
            border-radius: 5px;
            font-weight: bold;
        }
        QPushButton:hover { background-color: #1a4a7a; }
        QPushButton:pressed { background-color: #0a2a50; }
        QPushButton:disabled { background-color: #333; color: #666; }
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit {
            background-color: #0d0d1a;
            color: #e0e0f0;
            border: 1px solid #0f3460;
            padding: 6px;
            border-radius: 4px;
        }
        QLineEdit:focus, QTextEdit:focus, QComboBox:focus {
            border: 1px solid #e94560;
        }
        QListWidget {
            background-color: #0d0d1a;
            color: #e0e0f0;
            border: 1px solid #0f3460;
            border-radius: 4px;
        }
        QListWidget::item:selected {
            background-color: #e94560;
        }
        QListWidget::item:hover {
            background-color: #1a4a7a;
        }
        QLabel { color: #e0e0f0; }
        QGroupBox {
            color: #c0c0d0;
            border: 1px solid #0f3460;
            border-radius: 5px;
            margin-top: 12px;
            padding-top: 16px;
            font-weight: bold;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 5px;
        }
        QProgressBar {
            background-color: #0d0d1a;
            border: 1px solid #0f3460;
            border-radius: 4px;
            text-align: center;
            color: #e0e0f0;
            height: 22px;
        }
        QProgressBar::chunk {
            background-color: #e94560;
            border-radius: 3px;
        }
        QSlider::groove:horizontal {
            background: #0d0d1a;
            height: 6px;
            border-radius: 3px;
        }
        QSlider::handle:horizontal {
            background: #e94560;
            width: 16px;
            margin: -5px 0;
            border-radius: 8px;
        }
        QSlider::sub-page:horizontal {
            background: #0f3460;
            border-radius: 3px;
        }
        QCheckBox { color: #e0e0f0; }
        QCheckBox::indicator {
            width: 18px;
            height: 18px;
            border-radius: 3px;
            border: 1px solid #0f3460;
            background: #0d0d1a;
        }
        QCheckBox::indicator:checked {
            background: #e94560;
            border: 1px solid #e94560;
        }
        QScrollBar:vertical {
            background: #0d0d1a;
            width: 10px;
            border-radius: 5px;
        }
        QScrollBar::handle:vertical {
            background: #0f3460;
            min-height: 30px;
            border-radius: 5px;
        }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            height: 0px;
        }
        QStatusBar {
            background-color: #16213e;
            color: #c0c0d0;
        }
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Reddit Video Maker")
        self.setMinimumSize(1000, 750)

        # Core components
        self.fetcher = RedditFetcher()
        self.tts_engine = TTSEngine()
        self.composer = VideoComposer()

        # State
        self.stories: list[RedditStory] = []
        self.current_story: RedditStory | None = None
        self.current_comments: list[dict] = []
        self.audio_path: str | None = None
        self.word_boundaries: list[WordBoundary] = []
        self.video_output_path: str | None = None

        # Workers
        self.fetch_worker: FetchStoriesWorker | None = None
        self.comments_worker: FetchCommentsWorker | None = None
        self.tts_worker: TTSWorker | None = None
        self.tts_preview_worker: TTSPreviewWorker | None = None
        self.video_worker: VideoComposeWorker | None = None

        # Settings
        self.settings = QSettings("RedditVideoMaker", "RedditVideoMaker")

        # Working directory
        self.work_dir = os.path.join(
            os.path.expanduser('~'), '.reddit_video_maker'
        )
        os.makedirs(self.work_dir, exist_ok=True)

        # Media player (optional)
        self.audio_player = None
        self.audio_output = None
        self.video_player = None
        self.video_audio_output = None
        self._setup_media_players()

        # Build UI
        self._setup_ui()
        self.setStyleSheet(self.DARK_THEME)

        # Load saved settings
        self._load_settings()

        # Status
        self._set_status("Ready")

    def _setup_media_players(self):
        """Setup optional media players for preview."""
        if not HAS_MULTIMEDIA:
            return

        self.audio_player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.audio_player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.9)

        self.video_player = QMediaPlayer()
        self.video_audio_output = QAudioOutput()
        self.video_player.setAudioOutput(self.video_audio_output)
        self.video_audio_output.setVolume(0.9)

    # ------------------------------------------------------------------
    # UI Setup
    # ------------------------------------------------------------------

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(10, 10, 10, 10)

        # Header
        header = QHBoxLayout()
        title_label = QLabel("🎬 Reddit Video Maker")
        title_label.setStyleSheet(
            "font-size: 20px; font-weight: bold; color: #e94560; padding: 5px;"
        )
        header.addWidget(title_label)
        header.addStretch()

        settings_btn = QPushButton("⚙ Settings")
        settings_btn.clicked.connect(self._open_settings)
        header.addWidget(settings_btn)

        main_layout.addLayout(header)

        # Tab widget
        self.tabs = QTabWidget()
        self.tabs.addTab(self._create_story_tab(), "📋 Stories")
        self.tabs.addTab(self._create_settings_tab(), "🎙 Voice & Video")
        self.tabs.addTab(self._create_generate_tab(), "🎬 Generate")
        main_layout.addWidget(self.tabs)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        main_layout.addWidget(self.progress_bar)

        # Status bar
        self.statusBar().showMessage("Ready")

    def _create_story_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # Fetch controls
        fetch_group = QGroupBox("Fetch Stories")
        fetch_layout = QGridLayout(fetch_group)

        fetch_layout.addWidget(QLabel("Subreddit:"), 0, 0)
        self.subreddit_input = QLineEdit("AskReddit")
        self.subreddit_input.setPlaceholderText("e.g., AskReddit, AmItheAsshole, nosleep")
        fetch_layout.addWidget(self.subreddit_input, 0, 1)

        fetch_layout.addWidget(QLabel("Sort:"), 0, 2)
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Hot", "New", "Top", "Rising", "Controversial"])
        fetch_layout.addWidget(self.sort_combo, 0, 3)

        fetch_layout.addWidget(QLabel("Time:"), 1, 0)
        self.time_combo = QComboBox()
        self.time_combo.addItems(["Hour", "Day", "Week", "Month", "Year", "All"])
        self.time_combo.setCurrentText("Day")
        fetch_layout.addWidget(self.time_combo, 1, 1)

        fetch_layout.addWidget(QLabel("Limit:"), 1, 2)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(5, 100)
        self.limit_spin.setValue(25)
        fetch_layout.addWidget(self.limit_spin, 1, 3)

        self.fetch_btn = QPushButton("🔍 Fetch Stories")
        self.fetch_btn.clicked.connect(self._on_fetch_stories)
        fetch_layout.addWidget(self.fetch_btn, 2, 0, 1, 4)

        layout.addWidget(fetch_group)

        # Splitter: story list | story details
        splitter = QSplitter(Qt.Horizontal)

        # Story list
        list_group = QGroupBox("Stories")
        list_layout = QVBoxLayout(list_group)
        self.story_list = QListWidget()
        self.story_list.currentRowChanged.connect(self._on_story_selected)
        list_layout.addWidget(self.story_list)
        splitter.addWidget(list_group)

        # Story details
        details_group = QGroupBox("Story Details & Text Editor")
        details_layout = QVBoxLayout(details_group)

        # Story info
        info_layout = QGridLayout()
        info_layout.addWidget(QLabel("Title:"), 0, 0)
        self.story_title_label = QLabel("—")
        self.story_title_label.setWordWrap(True)
        self.story_title_label.setStyleSheet("font-weight: bold;")
        info_layout.addWidget(self.story_title_label, 0, 1)

        info_layout.addWidget(QLabel("Author:"), 1, 0)
        self.story_author_label = QLabel("—")
        info_layout.addWidget(self.story_author_label, 1, 1)

        info_layout.addWidget(QLabel("Score:"), 2, 0)
        self.story_score_label = QLabel("—")
        info_layout.addWidget(self.story_score_label, 2, 1)

        info_layout.addWidget(QLabel("Comments:"), 3, 0)
        self.story_comments_label = QLabel("—")
        info_layout.addWidget(self.story_comments_label, 3, 1)

        info_layout.addWidget(QLabel("Subreddit:"), 4, 0)
        self.story_subreddit_label = QLabel("—")
        info_layout.addWidget(self.story_subreddit_label, 4, 1)

        details_layout.addLayout(info_layout)

        # Text editor
        details_layout.addWidget(QLabel("Narration Text (editable):"))
        self.text_editor = QTextEdit()
        self.text_editor.setPlaceholderText(
            "Select a story to view and edit the narration text..."
        )
        details_layout.addWidget(self.text_editor)

        # Comment controls
        comment_layout = QHBoxLayout()
        self.fetch_comments_btn = QPushButton("💬 Fetch Top Comments")
        self.fetch_comments_btn.clicked.connect(self._on_fetch_comments)
        self.fetch_comments_btn.setEnabled(False)
        comment_layout.addWidget(self.fetch_comments_btn)

        self.append_comments_btn = QPushButton("➕ Append Comments to Text")
        self.append_comments_btn.clicked.connect(self._on_append_comments)
        self.append_comments_btn.setEnabled(False)
        comment_layout.addWidget(self.append_comments_btn)

        comment_layout.addStretch()
        details_layout.addLayout(comment_layout)

        # Comments list
        self.comments_list = QListWidget()
        self.comments_list.setMaximumHeight(120)
        details_layout.addWidget(QLabel("Top Comments:"))
        details_layout.addWidget(self.comments_list)

        splitter.addWidget(details_group)
        splitter.setSizes([350, 650])

        layout.addWidget(splitter)

        return tab

    def _create_settings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)

        # --- Voice Settings ---
        voice_group = QGroupBox("Voice & TTS Settings")
        voice_layout = QFormLayout(voice_group)

        self.voice_combo = QComboBox()
        voices = self.tts_engine.get_popular_voices()
        for voice_id, label in voices:
            self.voice_combo.addItem(label, voice_id)
        voice_layout.addRow("Voice:", self.voice_combo)

        # Refresh voices button
        refresh_voices_btn = QPushButton("🔄 Load All Voices")
        refresh_voices_btn.clicked.connect(self._on_refresh_voices)
        voice_layout.addRow("", refresh_voices_btn)

        # Rate slider
        rate_layout = QHBoxLayout()
        self.rate_slider = QSlider(Qt.Horizontal)
        self.rate_slider.setRange(-50, 50)
        self.rate_slider.setValue(0)
        self.rate_label = QLabel("+0%")
        self.rate_slider.valueChanged.connect(
            lambda v: self.rate_label.setText(f"{'+' if v >= 0 else ''}{v}%")
        )
        rate_layout.addWidget(self.rate_slider)
        rate_layout.addWidget(self.rate_label)
        voice_layout.addRow("Speed:", rate_layout)

        # Volume slider
        vol_layout = QHBoxLayout()
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(-50, 50)
        self.volume_slider.setValue(0)
        self.volume_label = QLabel("+0%")
        self.volume_slider.valueChanged.connect(
            lambda v: self.volume_label.setText(f"{'+' if v >= 0 else ''}{v}%")
        )
        vol_layout.addWidget(self.volume_slider)
        vol_layout.addWidget(self.volume_label)
        voice_layout.addRow("Volume:", vol_layout)

        # Preview
        preview_layout = QHBoxLayout()
        self.preview_text = QLineEdit("This is a preview of the selected voice.")
        preview_layout.addWidget(self.preview_text)
        self.preview_voice_btn = QPushButton("▶ Preview")
        self.preview_voice_btn.clicked.connect(self._on_preview_voice)
        preview_layout.addWidget(self.preview_voice_btn)
        voice_layout.addRow("Preview:", preview_layout)

        scroll_layout.addWidget(voice_group)

        # --- Video Settings ---
        video_group = QGroupBox("Video Settings")
        video_layout = QFormLayout(video_group)

        self.aspect_combo = QComboBox()
        self.aspect_combo.addItem("9:16 (Shorts/TikTok)", "9:16")
        self.aspect_combo.addItem("16:9 (YouTube)", "16:9")
        self.aspect_combo.addItem("1:1 (Square)", "1:1")
        video_layout.addRow("Aspect Ratio:", self.aspect_combo)

        # Background video
        bg_layout = QHBoxLayout()
        self.bg_video_edit = QLineEdit()
        self.bg_video_edit.setPlaceholderText("Select a background video file...")
        bg_layout.addWidget(self.bg_video_edit)
        bg_browse = QPushButton("📁 Browse")
        bg_browse.clicked.connect(self._browse_background)
        bg_layout.addWidget(bg_browse)
        video_layout.addRow("Background Video:", bg_layout)

        # Caption style
        self.caption_style_combo = QComboBox()
        self.caption_style_combo.addItem("Phrase (recommended)", "phrase")
        self.caption_style_combo.addItem("Word by Word", "word")
        self.caption_style_combo.addItem("Full Text", "full")
        video_layout.addRow("Caption Style:", self.caption_style_combo)

        self.words_per_phrase_spin = QSpinBox()
        self.words_per_phrase_spin.setRange(1, 8)
        self.words_per_phrase_spin.setValue(3)
        video_layout.addRow("Words per Phrase:", self.words_per_phrase_spin)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(30, 120)
        self.font_size_spin.setValue(60)
        video_layout.addRow("Caption Font Size:", self.font_size_spin)

        self.title_duration_spin = QDoubleSpinBox()
        self.title_duration_spin.setRange(1.0, 10.0)
        self.title_duration_spin.setValue(3.0)
        self.title_duration_spin.setSingleStep(0.5)
        self.title_duration_spin.setSuffix(" s")
        video_layout.addRow("Title Card Duration:", self.title_duration_spin)

        # Background music
        music_layout = QHBoxLayout()
        self.music_edit = QLineEdit()
        self.music_edit.setPlaceholderText("Optional background music file...")
        music_layout.addWidget(self.music_edit)
        music_browse = QPushButton("📁 Browse")
        music_browse.clicked.connect(self._browse_music)
        music_layout.addWidget(music_browse)
        video_layout.addRow("Background Music:", music_layout)

        self.music_volume_slider = QSlider(Qt.Horizontal)
        self.music_volume_slider.setRange(0, 100)
        self.music_volume_slider.setValue(12)
        self.music_volume_label = QLabel("12%")
        self.music_volume_slider.valueChanged.connect(
            lambda v: self.music_volume_label.setText(f"{v}%")
        )
        music_vol_layout = QHBoxLayout()
        music_vol_layout.addWidget(self.music_volume_slider)
        music_vol_layout.addWidget(self.music_volume_label)
        video_layout.addRow("Music Volume:", music_vol_layout)

        scroll_layout.addWidget(video_group)

        # --- Caption Appearance ---
        caption_group = QGroupBox("Caption Appearance")
        caption_layout = QFormLayout(caption_group)

        # Text color
        text_color_layout = QHBoxLayout()
        self.text_color_preview = QLabel()
        self.text_color_preview.setFixedSize(40, 25)
        self.text_color_preview.setStyleSheet("background-color: #FFFFFF; border: 1px solid #333;")
        self._text_color = "#FFFFFF"
        text_color_btn = QPushButton("Choose...")
        text_color_btn.clicked.connect(lambda: self._choose_color('text'))
        text_color_layout.addWidget(self.text_color_preview)
        text_color_layout.addWidget(text_color_btn)
        text_color_layout.addStretch()
        caption_layout.addRow("Text Color:", text_color_layout)

        # BG color
        bg_color_layout = QHBoxLayout()
        self.bg_color_preview = QLabel()
        self.bg_color_preview.setFixedSize(40, 25)
        self.bg_color_preview.setStyleSheet("background-color: #000000; border: 1px solid #333;")
        self._bg_color = "#000000"
        bg_color_btn = QPushButton("Choose...")
        bg_color_btn.clicked.connect(lambda: self._choose_color('bg'))
        bg_color_layout.addWidget(self.bg_color_preview)
        bg_color_layout.addWidget(bg_color_btn)
        bg_color_layout.addStretch()
        caption_layout.addRow("Caption BG Color:", bg_color_layout)

        self.bg_opacity_spin = QSpinBox()
        self.bg_opacity_spin.setRange(0, 255)
        self.bg_opacity_spin.setValue(160)
        caption_layout.addRow("BG Opacity:", self.bg_opacity_spin)

        scroll_layout.addWidget(caption_group)
        scroll_layout.addStretch()

        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)

        return tab

    def _create_generate_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # --- Audio Section ---
        audio_group = QGroupBox("Step 1: Generate Narration Audio")
        audio_layout = QVBoxLayout(audio_group)

        audio_status = QHBoxLayout()
        audio_status.addWidget(QLabel("Audio Status:"))
        self.audio_status_label = QLabel("⚠ Not generated")
        self.audio_status_label.setStyleSheet("color: #ffaa00; font-weight: bold;")
        audio_status.addWidget(self.audio_status_label)
        audio_status.addStretch()
        audio_layout.addLayout(audio_status)

        audio_buttons = QHBoxLayout()
        self.generate_audio_btn = QPushButton("🎙 Generate Audio")
        self.generate_audio_btn.setStyleSheet(
            "background-color: #0f3460; font-size: 14px; padding: 10px;"
        )
        self.generate_audio_btn.clicked.connect(self._on_generate_audio)
        audio_buttons.addWidget(self.generate_audio_btn)

        self.play_audio_btn = QPushButton("▶ Play Audio")
        self.play_audio_btn.setEnabled(False)
        self.play_audio_btn.clicked.connect(self._on_play_audio)
        audio_buttons.addWidget(self.play_audio_btn)

        audio_buttons.addStretch()
        audio_layout.addLayout(audio_buttons)

        layout.addWidget(audio_group)

        # --- Video Section ---
        video_group = QGroupBox("Step 2: Generate Video")
        video_layout = QVBoxLayout(video_group)

        # Output path
        out_layout = QHBoxLayout()
        out_layout.addWidget(QLabel("Output:"))
        self.output_path_edit = QLineEdit(
            os.path.join(os.path.expanduser('~'), 'reddit_video.mp4')
        )
        out_layout.addWidget(self.output_path_edit)
        out_browse = QPushButton("📁 Browse")
        out_browse.clicked.connect(self._browse_output)
        out_layout.addWidget(out_browse)
        video_layout.addLayout(out_layout)

        video_buttons = QHBoxLayout()
        self.generate_video_btn = QPushButton("🎬 Generate Video")
        self.generate_video_btn.setStyleSheet(
            "background-color: #e94560; font-size: 14px; padding: 12px;"
        )
        self.generate_video_btn.clicked.connect(self._on_generate_video)
        video_buttons.addWidget(self.generate_video_btn)

        self.open_video_btn = QPushButton("📂 Open Output Folder")
        self.open_video_btn.setEnabled(False)
        self.open_video_btn.clicked.connect(self._on_open_output_folder)
        video_buttons.addWidget(self.open_video_btn)

        video_buttons.addStretch()
        video_layout.addLayout(video_buttons)

        layout.addWidget(video_group)

        # --- Preview Section ---
        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)

        if HAS_MULTIMEDIA:
            self.video_preview_widget = QVideoWidget()
            self.video_preview_widget.setMinimumHeight(250)
            self.video_preview_widget.setStyleSheet("background-color: black;")
            self.video_player.setVideoOutput(self.video_preview_widget)
            preview_layout.addWidget(self.video_preview_widget)

            preview_controls = QHBoxLayout()
            self.play_video_btn = QPushButton("▶ Play Video")
            self.play_video_btn.setEnabled(False)
            self.play_video_btn.clicked.connect(self._on_play_video)
            preview_controls.addWidget(self.play_video_btn)

            self.stop_video_btn = QPushButton("⏹ Stop")
            self.stop_video_btn.setEnabled(False)
            self.stop_video_btn.clicked.connect(self._on_stop_video)
            preview_controls.addWidget(self.stop_video_btn)

            preview_controls.addStretch()
            preview_layout.addLayout(preview_controls)
        else:
            no_preview = QLabel(
                "QtMultimedia not available.\n"
                "Video preview is disabled. Use 'Open Output Folder' to view the video."
            )
            no_preview.setAlignment(Qt.AlignCenter)
            no_preview.setStyleSheet("color: #888; padding: 40px;")
            preview_layout.addWidget(no_preview)

        layout.addWidget(preview_group)
        layout.addStretch()

        return tab

    # ------------------------------------------------------------------
    # Story Tab Handlers
    # ------------------------------------------------------------------

    def _on_fetch_stories(self):
        subreddit = self.subreddit_input.text().strip()
        if not subreddit:
            QMessageBox.warning(self, "Warning", "Please enter a subreddit name.")
            return

        sort = self.sort_combo.currentText().lower()
        limit = self.limit_spin.value()
        time_filter = self.time_combo.currentText().lower()

        self.fetch_btn.setEnabled(False)
        self._set_status(f"Fetching stories from r/{subreddit}...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # Indeterminate

        self.fetch_worker = FetchStoriesWorker(
            self.fetcher, subreddit, sort, limit, time_filter
        )
        self.fetch_worker.stories_ready.connect(self._on_stories_fetched)
        self.fetch_worker.error.connect(self._on_fetch_error)
        self.fetch_worker.start()

    def _on_stories_fetched(self, stories: list):
        self.stories = stories
        self.story_list.clear()

        if not stories:
            QMessageBox.information(
                self, "No Stories",
                "No stories found. Try a different subreddit or sort method."
            )
            self._set_status("No stories found")
        else:
            for story in stories:
                item_text = (
                    f"⬆ {story.display_score}  💬 {story.num_comments}  |  "
                    f"{story.display_title}"
                )
                item = QListWidgetItem(item_text)
                self.story_list.addItem(item)

            self._set_status(f"Found {len(stories)} stories")

        self.fetch_btn.setEnabled(True)
        self.progress_bar.setVisible(False)

    def _on_fetch_error(self, error: str):
        self.fetch_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self._set_status("Error fetching stories")
        QMessageBox.critical(self, "Error", f"Failed to fetch stories:\n{error}")

    def _on_story_selected(self, row: int):
        if row < 0 or row >= len(self.stories):
            return

        self.current_story = self.stories[row]
        s = self.current_story

        self.story_title_label.setText(s.title)
        self.story_author_label.setText(f"u/{s.author}")
        self.story_score_label.setText(s.display_score)
        self.story_comments_label.setText(str(s.num_comments))
        self.story_subreddit_label.setText(f"r/{s.subreddit}")

        # Set narration text
        narration = s.narration_text
        self.text_editor.setPlainText(narration)

        # Enable comment fetch
        self.fetch_comments_btn.setEnabled(True)
        self.append_comments_btn.setEnabled(False)
        self.comments_list.clear()
        self.current_comments = []

        # Reset audio state
        self.audio_path = None
        self.word_boundaries = []
        self.audio_status_label.setText("⚠ Not generated")
        self.audio_status_label.setStyleSheet("color: #ffaa00; font-weight: bold;")
        self.play_audio_btn.setEnabled(False)

    def _on_fetch_comments(self):
        if not self.current_story:
            return

        self.fetch_comments_btn.setEnabled(False)
        self._set_status("Fetching comments...")

        self.comments_worker = FetchCommentsWorker(
            self.fetcher,
            self.current_story.subreddit,
            self.current_story.id
        )
        self.comments_worker.comments_ready.connect(self._on_comments_fetched)
        self.comments_worker.error.connect(self._on_comments_error)
        self.comments_worker.start()

    def _on_comments_fetched(self, comments: list):
        self.current_comments = comments
        self.comments_list.clear()

        for comment in comments[:20]:
            text = f"⬆ {comment['score']} | u/{comment['author']}: {comment['body'][:100]}..."
            self.comments_list.addItem(text)

        if comments:
            self.append_comments_btn.setEnabled(True)

        self.fetch_comments_btn.setEnabled(True)
        self._set_status(f"Fetched {len(comments)} comments")

    def _on_comments_error(self, error: str):
        self.fetch_comments_btn.setEnabled(True)
        self._set_status("Error fetching comments")
        QMessageBox.warning(self, "Warning", f"Failed to fetch comments:\n{error}")

    def _on_append_comments(self):
        if not self.current_comments:
            return

        current_text = self.text_editor.toPlainText()
        comment_text = "\n\n"
        for i, comment in enumerate(self.current_comments[:5], 1):
            comment_text += f"Top comment {i} by {comment['author']}: {comment['body']}\n\n"

        self.text_editor.setPlainText(current_text + comment_text)
        self._set_status("Comments appended to narration text")

    # ------------------------------------------------------------------
    # Settings Tab Handlers
    # ------------------------------------------------------------------

    def _on_refresh_voices(self):
        self._set_status("Loading all available voices...")
        QApplication.processEvents()

        voices = self.tts_engine.get_all_voices()
        self.voice_combo.clear()
        for voice_id, label in voices:
            self.voice_combo.addItem(label, voice_id)

        self._set_status(f"Loaded {len(voices)} voices")

    def _on_preview_voice(self):
        voice_id = self.voice_combo.currentData()
        text = self.preview_text.text().strip()
        if not text:
            text = "This is a voice preview."

        preview_path = os.path.join(self.work_dir, 'voice_preview.mp3')

        self.preview_voice_btn.setEnabled(False)
        self._set_status("Generating voice preview...")

        self.tts_preview_worker = TTSPreviewWorker(
            self.tts_engine, text, voice_id, preview_path
        )
        self.tts_preview_worker.preview_ready.connect(self._on_preview_ready)
        self.tts_preview_worker.error.connect(self._on_preview_error)
        self.tts_preview_worker.start()

    def _on_preview_ready(self, path: str):
        self.preview_voice_btn.setEnabled(True)
        self._set_status("Voice preview ready")

        if self.audio_player and HAS_MULTIMEDIA:
            self.audio_player.setSource(QUrl.fromLocalFile(path))
            self.audio_player.play()

    def _on_preview_error(self, error: str):
        self.preview_voice_btn.setEnabled(True)
        self._set_status("Preview error")
        QMessageBox.warning(self, "Preview Error", error)

    def _browse_background(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Background Video",
            "",
            "Video Files (*.mp4 *.avi *.mov *.mkv *.webm);;All Files (*.*)"
        )
        if path:
            self.bg_video_edit.setText(path)
            self.settings.setValue('paths/last_bg_video', path)

    def _browse_music(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Background Music",
            "",
            "Audio Files (*.mp3 *.wav *.aac *.m4a *.ogg);;All Files (*.*)"
        )
        if path:
            self.music_edit.setText(path)
            self.settings.setValue('paths/last_music', path)

    def _choose_color(self, target: str):
        if target == 'text':
            color = QColorDialog.getColor(QColor(self._text_color), self, "Text Color")
            if color.isValid():
                self._text_color = color.name()
                self.text_color_preview.setStyleSheet(
                    f"background-color: {self._text_color}; border: 1px solid #333;"
                )
        elif target == 'bg':
            color = QColorDialog.getColor(QColor(self._bg_color), self, "Background Color")
            if color.isValid():
                self._bg_color = color.name()
                self.bg_color_preview.setStyleSheet(
                    f"background-color: {self._bg_color}; border: 1px solid #333;"
                )

    # ------------------------------------------------------------------
    # Generate Tab Handlers
    # ------------------------------------------------------------------

    def _on_generate_audio(self):
        text = self.text_editor.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Warning", "No text to narrate. Please select a story.")
            return

        if not self.current_story:
            QMessageBox.warning(self, "Warning", "Please select a story first.")
            return

        voice_id = self.voice_combo.currentData()
        rate_val = self.rate_slider.value()
        rate = f"{'+' if rate_val >= 0 else ''}{rate_val}%"
        volume_val = self.volume_slider.value()
        volume = f"{'+' if volume_val >= 0 else ''}{volume_val}%"

        audio_path = os.path.join(self.work_dir, f"narration_{self.current_story.id}.mp3")

        self.generate_audio_btn.setEnabled(False)
        self.audio_status_label.setText("⏳ Generating...")
        self.audio_status_label.setStyleSheet("color: #e94560; font-weight: bold;")
        self._set_status("Generating audio (this may take a moment)...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)

        self.tts_worker = TTSWorker(
            self.tts_engine, text, voice_id, audio_path, rate, volume
        )
        self.tts_worker.audio_ready.connect(self._on_audio_ready)
        self.tts_worker.error.connect(self._on_audio_error)
        self.tts_worker.start()

    def _on_audio_ready(self, audio_path: str, boundaries: list):
        self.audio_path = audio_path
        self.word_boundaries = boundaries

        duration = boundaries[-1].end if boundaries else 0
        self.audio_status_label.setText(
            f"✅ Ready ({duration:.1f}s, {len(boundaries)} words)"
        )
        self.audio_status_label.setStyleSheet("color: #00ff88; font-weight: bold;")
        self.play_audio_btn.setEnabled(True)
        self.generate_audio_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self._set_status("Audio generated successfully")

    def _on_audio_error(self, error: str):
        self.generate_audio_btn.setEnabled(True)
        self.audio_status_label.setText("❌ Error")
        self.audio_status_label.setStyleSheet("color: #ff4444; font-weight: bold;")
        self.progress_bar.setVisible(False)
        self._set_status("Audio generation failed")
        QMessageBox.critical(self, "Error", f"Failed to generate audio:\n{error}")

    def _on_play_audio(self):
        if not self.audio_path:
            return

        if self.audio_player and HAS_MULTIMEDIA:
            self.audio_player.setSource(QUrl.fromLocalFile(self.audio_path))
            self.audio_player.play()
            self._set_status("Playing audio preview...")
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.audio_path))

    def _on_generate_video(self):
        if not self.audio_path:
            QMessageBox.warning(
                self, "Warning",
                "No audio generated. Please generate narration audio first."
            )
            self.tabs.setCurrentIndex(2)
            return

        if not self.current_story:
            QMessageBox.warning(self, "Warning", "No story selected.")
            return

        output_path = self.output_path_edit.text().strip()
        if not output_path:
            QMessageBox.warning(self, "Warning", "Please specify an output file path.")
            return

        # Collect all parameters
        params = {
            'story_title': self.current_story.title,
            'story_body': self.current_story.body,
            'subreddit': self.current_story.subreddit,
            'author': self.current_story.author,
            'audio_path': self.audio_path,
            'word_boundaries': self.word_boundaries,
            'background_path': self.bg_video_edit.text().strip() or None,
            'output_path': output_path,
            'aspect_ratio': self.aspect_combo.currentData(),
            'caption_style': self.caption_style_combo.currentData(),
            'words_per_phrase': self.words_per_phrase_spin.value(),
            'title_duration': self.title_duration_spin.value(),
            'background_music': self.music_edit.text().strip() or None,
            'music_volume': self.music_volume_slider.value() / 100.0,
            'font_size': self.font_size_spin.value(),
            'text_color': self._text_color,
            'bg_color': self._bg_color,
            'bg_opacity': self.bg_opacity_spin.value(),
        }

        self.generate_video_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._set_status("Generating video...")

        self.video_worker = VideoComposeWorker(self.composer, params)
        self.video_worker.progress.connect(self._on_video_progress)
        self.video_worker.video_ready.connect(self._on_video_ready)
        self.video_worker.error.connect(self._on_video_error)
        self.video_worker.start()

    def _on_video_progress(self, pct: int, msg: str):
        self.progress_bar.setValue(pct)
        self._set_status(f"{msg} ({pct}%)")

    def _on_video_ready(self, output_path: str):
        self.video_output_path = output_path
        self.progress_bar.setVisible(False)
        self.generate_video_btn.setEnabled(True)
        self.open_video_btn.setEnabled(True)
        self.play_video_btn.setEnabled(True)
        self.stop_video_btn.setEnabled(True)
        self._set_status(f"Video saved: {output_path}")

        QMessageBox.information(
            self, "Success",
            f"Video generated successfully!\n\nSaved to:\n{output_path}"
        )

    def _on_video_error(self, error: str):
        self.progress_bar.setVisible(False)
        self.generate_video_btn.setEnabled(True)
        self._set_status("Video generation failed")
        QMessageBox.critical(
            self, "Error",
            f"Failed to generate video:\n{error}\n\n"
            f"Make sure FFmpeg is installed and all file paths are correct."
        )

    def _on_play_video(self):
        if not self.video_output_path or not os.path.exists(self.video_output_path):
            return

        if self.video_player and HAS_MULTIMEDIA:
            self.video_player.setSource(QUrl.fromLocalFile(self.video_output_path))
            self.video_player.play()
            self._set_status("Playing video preview...")
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.video_output_path))

    def _on_stop_video(self):
        if self.video_player:
            self.video_player.stop()
            self._set_status("Stopped")

    def _on_open_output_folder(self):
        path = self.output_path_edit.text().strip()
        if path:
            folder = os.path.dirname(path) or '.'
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _browse_output(self):
        default_name = "reddit_video.mp4"
        if self.current_story:
            safe_title = ''.join(
                c for c in self.current_story.title[:40] if c.isalnum() or c in ' -_'
            ).strip()
            if safe_title:
                default_name = f"reddit_{safe_title}.mp4"

        default_dir = self.settings.value(
            'paths/output_dir', os.path.expanduser('~')
        )

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Video As",
            os.path.join(default_dir, default_name),
            "Video Files (*.mp4);;All Files (*.*)"
        )
        if path:
            self.output_path_edit.setText(path)

    # ------------------------------------------------------------------
    # Utility Methods
    # ------------------------------------------------------------------

    def _set_status(self, msg: str):
        self.statusBar().showMessage(msg)

    def _open_settings(self):
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.Accepted:
            dialog.save_settings()
            self._load_settings()

    def _load_settings(self):
        # Restore last used values
        last_sub = self.settings.value('reddit/last_subreddit', 'AskReddit')
        self.subreddit_input.setText(last_sub)

        last_bg = self.settings.value('paths/last_bg_video', '')
        if last_bg:
            self.bg_video_edit.setText(last_bg)

        last_music = self.settings.value('paths/last_music', '')
        if last_music:
            self.music_edit.setText(last_music)

        last_voice = self.settings.value('tts/voice', '')
        if last_voice:
            idx = self.voice_combo.findData(last_voice)
            if idx >= 0:
                self.voice_combo.setCurrentIndex(idx)

    def _save_settings(self):
        if self.current_story:
            self.settings.setValue('reddit/last_subreddit', self.subreddit_input.text())
        self.settings.setValue('tts/voice', self.voice_combo.currentData())
        self.settings.setValue('paths/last_bg_video', self.bg_video_edit.text())
        self.settings.setValue('paths/last_music', self.music_edit.text())

    def closeEvent(self, event):
        self._save_settings()

        # Stop any running workers
        for worker in [self.fetch_worker, self.comments_worker,
                       self.tts_worker, self.tts_preview_worker,
                       self.video_worker]:
            if worker and worker.isRunning():
                worker.quit()
                worker.wait(3000)

        # Stop media players
        if self.audio_player:
            self.audio_player.stop()
        if self.video_player:
            self.video_player.stop()

        event.accept()


# ============================================================================
# Entry Point
# ============================================================================

def main():
    # Check for required dependencies
    missing = []
    try:
        import edge_tts
    except ImportError:
        missing.append("edge-tts")
    try:
        from moviepy.editor import VideoFileClip
    except ImportError:
        missing.append("moviepy")
    try:
        from PIL import Image
    except ImportError:
        missing.append("Pillow")

    if missing:
        print("Missing required packages:", ", ".join(missing))
        print("Install with: pip install", " ".join(missing))
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setStyle(QStyleFactory.create('Fusion'))

    # Apply dark palette
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(26, 26, 46))
    palette.setColor(QPalette.WindowText, QColor(224, 224, 240))
    palette.setColor(QPalette.Base, QColor(13, 13, 26))
    palette.setColor(QPalette.AlternateBase, QColor(22, 33, 62))
    palette.setColor(QPalette.Text, QColor(224, 224, 240))
    palette.setColor(QPalette.Button, QColor(15, 52, 96))
    palette.setColor(QPalette.ButtonText, QColor(224, 224, 240))
    palette.setColor(QPalette.Highlight, QColor(233, 69, 96))
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
#!/usr/bin/env python3
"""
Reddit Story YouTube Video Generator 
================================================================
- Thread Safety: Safely terminates background workers on window close.
- Robust UI: Fixed crashes when editing empty comment cells.
- Clean Code: Replaced fragile string parsing with Qt ItemData for TTS rates.
- Added cross-platform open_file helper to reduce code duplication.
"""

import sys
import os
import re
import json
import asyncio
import textwrap
import subprocess
import platform
import logging
import shutil
import bisect
import time
import hashlib
import html
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION & DYNAMIC LAYOUT
# ═══════════════════════════════════════════════════════════════════════════════

class Config:
    ASPECT_RATIO = "9:16"
    WIDTH = 1080
    HEIGHT = 1920
    FPS = 30

    TTS_VOICE = "en-US-GuyNeural"
    COMMENT_VOICE = "en-US-AriaNeural"
    TTS_RATE = "+10%"

    BG_VIDEO = None
    BG_VIDEO_VOLUME = 0.0
    BG_MUSIC = None
    BG_MUSIC_VOLUME = 0.15
    DARKEN_BG = False

    FONT_SIZE = 52
    TEXT_COLOR = (255, 255, 255)
    HIGHLIGHT_COLOR = (255, 215, 0)
    READ_COLOR = (130, 130, 130)
    TEXT_BG_ALPHA = 160
    WORDS_PER_SCREEN = 10

    SCREENSHOT_Y = 80
    SCREENSHOT_MAX_WIDTH = 960
    SCREENSHOT_MAX_HEIGHT = 650
    TEXT_Y = 1050
    TEXT_HEIGHT = 550
    HEADER_Y = 20

    INTRO_DURATION = 2.5
    OUTRO_DURATION = 2.0
    FADE_DURATION = 0.5
    COMMENT_GAP = 0.4  # Seconds of silence between post and comments

    WATERMARK = None
    OUTPUT_DIR = Path("output")
    TEMP_DIR = Path("output/temp")
    LOG_DIR = Path("logs")
    MAX_PART_DURATION = None
    PROFILE_PATH = Path("profile.json")
    CLEANUP_TEMP = True
    AUTO_OPEN_OUTPUT = False

    VIDEO_CODEC = "libx264"
    AUDIO_CODEC = "aac"
    VIDEO_PRESET = "medium"
    VIDEO_BITRATE = "5M"
    THREADS = os.cpu_count() or 2


def apply_aspect_ratio(ratio: str) -> None:
    Config.ASPECT_RATIO = ratio
    if ratio == "9:16":
        Config.WIDTH, Config.HEIGHT = 1080, 1920
        Config.SCREENSHOT_Y = 80
        Config.SCREENSHOT_MAX_WIDTH = 960
        Config.SCREENSHOT_MAX_HEIGHT = 650
        Config.TEXT_Y = 1050
        Config.TEXT_HEIGHT = 550
        Config.FONT_SIZE = 52
    else:
        Config.WIDTH, Config.HEIGHT = 1920, 1080
        Config.SCREENSHOT_Y = 40
        Config.SCREENSHOT_MAX_WIDTH = 1200
        Config.SCREENSHOT_MAX_HEIGHT = 500
        Config.TEXT_Y = 560
        Config.TEXT_HEIGHT = 420
        Config.FONT_SIZE = 46

# ═══════════════════════════════════════════════════════════════════════════════
# LOGGING & DEPENDENCIES
# ═══════════════════════════════════════════════════════════════════════════════

def setup_file_logging() -> Path:
    Config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = Config.LOG_DIR / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        filename=str(log_file),
        level=logging.DEBUG,
        format='%(asctime)s [%(levelname)s] %(message)s',
        force=True,
    )
    return log_file

def check_ffmpeg() -> bool:
    try:
        cmd = "ffmpeg" if platform.system() != "Windows" else "ffmpeg.exe"
        subprocess.run([cmd, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def check_python_dependencies() -> List[str]:
    missing = []
    try: import moviepy
    except ImportError: missing.append("moviepy")
    try: import edge_tts
    except ImportError: missing.append("edge-tts")
    try: import requests
    except ImportError: missing.append("requests")
    try: from PIL import Image
    except ImportError: missing.append("Pillow")
    try: import numpy
    except ImportError: missing.append("numpy")
    try: from PySide6 import QtCore
    except ImportError: missing.append("PySide6")
    return missing

def open_in_os(path: str) -> None:
    """Cross-platform method to open a file or directory in the OS file explorer."""
    if platform.system() == "Windows": 
        os.startfile(path)
    elif platform.system() == "Darwin": 
        subprocess.run(["open", path])
    else: 
        subprocess.run(["xdg-open", path])

# ═══════════════════════════════════════════════════════════════════════════════
# CORE BACKEND LOGIC
# ═══════════════════════════════════════════════════════════════════════════════

def sanitize_text_for_tts(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r'http\S+|www\.\S+', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'[*_#>|]+', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def validate_reddit_url(url: str) -> bool:
    pattern = r'^https?://(www\.)?reddit\.com/r/\w+/comments/\w+'
    return bool(re.match(pattern, url, re.IGNORECASE))

def fetch_reddit_post(url: str, num_comments: int = 1, max_retries: int = 3) -> Dict:
    if not validate_reddit_url(url):
        raise ValueError("Invalid Reddit URL.")

    json_url = url.split("?")[0].rstrip("/") + ".json"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RedditVideoBot/4.0"}

    for attempt in range(max_retries):
        try:
            response = requests.get(json_url, headers=headers, timeout=15)
            if response.status_code == 429:
                time.sleep((attempt + 1) * 3)
                continue
            response.raise_for_status()
            data = response.json()
            break
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1: time.sleep((attempt + 1) * 2)
            else: raise RuntimeError(f"Failed to fetch after {max_retries} attempts: {e}")

    post_data = data[0]["data"]["children"][0]["data"]

    def format_upvotes(ups: int) -> str:
        if ups >= 1_000_000: return f"{ups / 1_000_000:.1f}M"
        elif ups >= 1000: return f"{ups // 1000}k"
        return str(ups)

    story = {
        "title": post_data.get("title", "Unknown Title"),
        "body": post_data.get("selftext", "(No body text)"),
        "subreddit": f"r/{post_data.get('subreddit', 'AskReddit')}",
        "author": f"u/{post_data.get('author', 'anonymous')}",
        "upvotes": format_upvotes(post_data.get('ups', 0)),
        "comment_count": post_data.get('num_comments', 0),
        "post_id": post_data.get("id", f"fetch_{hash(url) % 10000:04d}"),
        "comments": []
    }

    raw_comments = data[1]["data"]["children"] if len(data) > 1 else []
    for c in raw_comments:
        if len(story["comments"]) >= num_comments: break
        cdata = c.get("data", {})
        if cdata.get("body") and cdata.get("author") != "AutoModerator":
            story["comments"].append({
                "author": f"u/{cdata.get('author', 'anonymous')}",
                "body": cdata["body"],
                "upvotes": format_upvotes(cdata.get('ups', 0)),
            })
    return story

async def _generate_tts_async(text: str, output_path, voice: str, rate: str) -> Tuple[str, List[Dict]]:
    import edge_tts
    timings = []
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    with open(output_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                timings.append({
                    "word": chunk["text"],
                    "offset": chunk["offset"] / 10_000_000,
                    "duration": chunk["duration"] / 10_000_000,
                })
    return str(output_path), timings

def generate_tts(text: str, output_path, voice: str, rate: str, max_retries: int = 2) -> Tuple[str, List[Dict]]:
    for attempt in range(max_retries):
        try:
            return asyncio.run(_generate_tts_async(text, output_path, voice, rate))
        except Exception as e:
            logging.warning(f"TTS attempt {attempt+1} failed: {e}")
            if attempt < max_retries - 1: time.sleep(2)
    raise RuntimeError(f"TTS generation failed after {max_retries} attempts.")

def _load_font(size: int, bold: bool = True):
    from PIL import ImageFont
    candidates = []
    if platform.system() == "Windows":
        candidates = ["arialbd" if bold else "arial", "segoeuib" if bold else "segoeui"]
    elif platform.system() == "Darwin":
        candidates = ["Arial Bold" if bold else "Arial", "Helvetica Bold" if bold else "Helvetica"]
    else:
        candidates = ["DejaVuSans-Bold" if bold else "DejaVuSans", "LiberationSans-Bold" if bold else "LiberationSans"]

    for name in candidates:
        try: return ImageFont.truetype(name, size)
        except (OSError, IOError): continue

    font_dirs = [Path("/usr/share/fonts"), Path("C:/Windows/Fonts")]
    for d in font_dirs:
        if d.exists():
            for fp in d.rglob("*.ttf"):
                if (bold and "bold" in fp.name.lower()) or not bold:
                    try: return ImageFont.truetype(str(fp), size)
                    except: continue
    return ImageFont.load_default()

def create_reddit_screenshot(data: Dict, output_path, is_comment: bool = False) -> str:
    from PIL import Image, ImageDraw

    card_width = 940
    card_margin = 24
    content_x = card_margin + 16

    body_len = len(data.get("body", ""))
    body_font_size = 24
    body_wrap_width = 55
    if body_len > 1000:
        body_font_size = 20
        body_wrap_width = 65
    elif body_len > 500:
        body_font_size = 22
        body_wrap_width = 60

    font_title = _load_font(32, bold=True)
    font_body = _load_font(body_font_size, bold=False)
    font_meta = _load_font(18, bold=True)
    font_sub = _load_font(26, bold=True)
    font_btn = _load_font(22, bold=True)

    title_lines = textwrap.wrap(data.get("title", ""), width=40) if not is_comment else []
    body_lines = textwrap.wrap(data.get("body", "(No body)"), width=body_wrap_width)
    if len(body_lines) > 25: body_lines = body_lines[:25] + ["[...]"]

    y = card_margin + 16
    y += 80
    if title_lines: y += len(title_lines) * 44 + 16
    y += len(body_lines) * (body_font_size + 10) + 20
    y += 60
    card_height = y + card_margin

    bg_color = (26, 26, 27)
    card_bg = (34, 34, 35)
    text_main = (215, 218, 220)
    text_meta = (129, 131, 137)
    reddit_orange = (255, 69, 0)

    img = Image.new("RGB", (card_width, card_height), bg_color)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, card_width - 1, card_height - 1], radius=12, fill=card_bg)

    y = card_margin + 16
    draw.ellipse([content_x, y, content_x + 40, y + 40], fill=reddit_orange)
    draw.text((content_x + 50, y + 2), data.get("subreddit", "r/AskReddit"), fill=text_main, font=font_sub)

    meta_text = f"Posted by {data.get('author', 'u/anonymous')}  •  {data.get('upvotes', '0')} upvotes"
    if is_comment:
        meta_text = f"{data.get('author', 'u/anonymous')}  •  {data.get('upvotes', '0')} upvotes  •  Top Comment"
    draw.text((content_x, y + 48), meta_text, fill=text_meta, font=font_meta)
    y += 80

    for line in title_lines:
        draw.text((content_x, y), line, fill=text_main, font=font_title)
        y += 44
    if title_lines: y += 12

    for line in body_lines:
        draw.text((content_x, y), line, fill=text_main, font=font_body)
        y += (body_font_size + 10)

    footer_y = card_height - card_margin - 40
    draw.line([(content_x, footer_y - 10), (card_width - card_margin - 16, footer_y - 10)], fill=(50, 50, 51), width=1)
    draw.polygon([(content_x, footer_y + 5), (content_x + 10, footer_y + 15), (content_x + 20, footer_y + 5)], fill=reddit_orange)
    draw.text((content_x + 30, footer_y), data.get("upvotes", "0"), fill=reddit_orange, font=font_btn)

    cmt_x = content_x + 120
    draw.rounded_rectangle([cmt_x, footer_y, cmt_x + 20, footer_y + 20], radius=4, outline=text_meta, width=2)
    draw.text((cmt_x + 30, footer_y), "Comment", fill=text_meta, font=font_btn)

    shr_x = content_x + 280
    draw.line([(shr_x, footer_y), (shr_x + 15, footer_y + 10)], fill=text_meta, width=2)
    draw.line([(shr_x + 15, footer_y + 10), (shr_x + 5, footer_y + 20)], fill=text_meta, width=2)
    draw.text((shr_x + 30, footer_y), "Share", fill=text_meta, font=font_btn)

    img.save(str(output_path))
    return str(output_path)

def generate_srt(word_timings: List[Dict], output_path, offset: float = 0.0) -> None:
    if not word_timings:
        Path(output_path).write_text("", encoding="utf-8")
        return

    def format_time(seconds: float) -> str:
        hrs = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        ms = int((seconds - int(seconds)) * 1000)
        return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"

    subtitle_index = 1
    with open(output_path, "w", encoding="utf-8") as f:
        chunk = []
        start_time = word_timings[0]["offset"] + offset
        end_time = start_time

        for i, w in enumerate(word_timings):
            chunk.append(w["word"])
            end_time = w["offset"] + w["duration"] + offset
            should_flush = (len(chunk) >= 7 or (i < len(word_timings) - 1 and word_timings[i + 1]["offset"] - w["offset"] > 0.5))

            if should_flush:
                f.write(f"{subtitle_index}\n{format_time(start_time)} --> {format_time(end_time)}\n{' '.join(chunk)}\n\n")
                subtitle_index += 1
                chunk = []
                if i < len(word_timings) - 1:
                    start_time = word_timings[i + 1]["offset"] + offset

        if chunk:
            f.write(f"{subtitle_index}\n{format_time(start_time)} --> {format_time(end_time)}\n{' '.join(chunk)}\n\n")

# ═══════════════════════════════════════════════════════════════════════════════
# VIDEO COMPOSITION
# ═══════════════════════════════════════════════════════════════════════════════

from moviepy import (
    VideoFileClip, AudioFileClip, ImageClip, ColorClip,
    CompositeVideoClip, concatenate_videoclips, concatenate_audioclips,
    CompositeAudioClip, VideoClip, AudioArrayClip
)
from moviepy import vfx
from PIL import Image, ImageDraw
import numpy as np

def _layout_words(draw, words, font, max_width):
    lines, current_line, current_width = [], [], 0
    space_px = 12
    for idx, word in enumerate(words):
        bbox = draw.textbbox((0, 0), word, font=font, stroke_width=2)
        w = bbox[2] - bbox[0] + space_px
        if current_width + w > max_width and current_line:
            lines.append(current_line); current_line, current_width = [], 0
        current_line.append((word, idx)); current_width += w
    if current_line: lines.append(current_line)
    return lines

def _render_text_frame(chunk_words, highlight_idx, font) -> np.ndarray:
    dummy_img = Image.new("RGBA", (10, 10))
    dummy_draw = ImageDraw.Draw(dummy_img)
    max_allowed_w = Config.WIDTH - 120
    current_font = font
    word_widths = {}
    
    for w in set(chunk_words):
        bbox = dummy_draw.textbbox((0, 0), w, font=current_font, stroke_width=2)
        w_w = bbox[2] - bbox[0]
        while w_w > max_allowed_w and current_font.size > 20:
            current_font = _load_font(current_font.size - 2, bold=True)
            bbox = dummy_draw.textbbox((0, 0), w, font=current_font, stroke_width=2)
            w_w = bbox[2] - bbox[0]
        word_widths[w] = w_w

    img = Image.new("RGBA", (Config.WIDTH, Config.TEXT_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([20, 0, Config.WIDTH - 20, Config.TEXT_HEIGHT - 1], radius=16, fill=(0, 0, 0, Config.TEXT_BG_ALPHA))

    lines = _layout_words(draw, chunk_words, current_font, Config.WIDTH - 80)
    line_height = current_font.size + 18
    total_h = len(lines) * line_height
    start_y = (Config.TEXT_HEIGHT - total_h) // 2

    for line_idx, line in enumerate(lines):
        total_line_w = sum(word_widths.get(w, 0) + 12 for w, _ in line) - 12
        x = (Config.WIDTH - total_line_w) // 2
        y = start_y + line_idx * line_height

        for word, w_idx in line:
            ww = word_widths.get(word, 0)
            if w_idx == highlight_idx:
                draw.rounded_rectangle([x - 6, y - 4, x + ww + 6, y + current_font.size + 6], radius=8, fill=(255, 215, 0, 50))
                color = Config.HIGHLIGHT_COLOR
            elif w_idx < highlight_idx: color = Config.READ_COLOR
            else: color = Config.TEXT_COLOR
            draw.text((x, y), word, fill=color, font=current_font, stroke_width=2, stroke_fill=(0, 0, 0))
            x += ww + 12
    return np.array(img)

def create_text_overlay_clip(text, word_timings, total_duration, intro_offset, start_offset=0.0):
    words = text.split()
    if not words or not word_timings:
        empty = np.zeros((Config.TEXT_HEIGHT, Config.WIDTH, 4), dtype=np.uint8)
        return ImageClip(empty, duration=total_duration)

    font = _load_font(Config.FONT_SIZE, bold=True)
    effective_len = min(len(words), len(word_timings))
    num_chunks = (effective_len + Config.WORDS_PER_SCREEN - 1) // Config.WORDS_PER_SCREEN

    pre_rendered = []
    for chunk_idx in range(num_chunks):
        chunk_start = chunk_idx * Config.WORDS_PER_SCREEN
        chunk_end = min(chunk_start + Config.WORDS_PER_SCREEN, effective_len)
        chunk_words = words[chunk_start:chunk_end]
        chunk_timings = word_timings[chunk_start:chunk_end]
        if not chunk_timings: continue

        for local_idx in range(len(chunk_words)):
            word_start_time = chunk_timings[local_idx]["offset"] + start_offset + intro_offset
            if local_idx < len(chunk_timings) - 1:
                word_end_time = chunk_timings[local_idx + 1]["offset"] + start_offset + intro_offset
            else:
                word_end_time = word_start_time + chunk_timings[local_idx]["duration"]
            word_end_time = max(word_end_time, word_start_time + 0.05)

            frame = _render_text_frame(chunk_words, local_idx, font)
            pre_rendered.append((word_start_time, word_end_time, frame))

    if not pre_rendered:
        empty = np.zeros((Config.TEXT_HEIGHT, Config.WIDTH, 4), dtype=np.uint8)
        return ImageClip(empty, duration=total_duration)

    start_times = np.array([p[0] for p in pre_rendered])
    end_times = np.array([p[1] for p in pre_rendered])
    frames = [p[2] for p in pre_rendered]
    transparent = np.zeros((Config.TEXT_HEIGHT, Config.WIDTH, 4), dtype=np.uint8)

    def make_frame(t):
        if t < start_times[0] or t > end_times[-1]: return transparent
        idx = bisect.bisect_right(start_times, t) - 1
        if idx < 0: idx = 0
        if idx >= len(frames): idx = len(frames) - 1
        return frames[idx]

    return VideoClip(make_frame, duration=total_duration)

def create_header_clip(story, duration):
    img = Image.new("RGBA", (Config.WIDTH, 70), (0, 0, 0, 220))
    draw = ImageDraw.Draw(img)
    font = _load_font(30, bold=True)
    comment_count = story.get("comment_count", len(story.get("comments", [])))
    text = f"{story.get('subreddit', 'r/AskReddit')}   ⬆ {story.get('upvotes', '0')}   💬 {comment_count}"
    tw = draw.textbbox((0, 0), text, font=font)[2]
    draw.text(((Config.WIDTH - tw) // 2, 18), text, fill=(255, 255, 255), font=font)
    return ImageClip(np.array(img), duration=duration)

def create_watermark_clip(text, duration):
    img = Image.new("RGBA", (Config.WIDTH, 60), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(24, bold=True)
    tw = draw.textbbox((0, 0), text, font=font)[2]
    x = Config.WIDTH - tw - 40
    draw.text((x, 15), text, fill=(255, 255, 255, 100), font=font, stroke_width=1, stroke_fill=(0, 0, 0, 100))
    return ImageClip(np.array(img), duration=duration).with_position(("right", "bottom"))

def create_darken_overlay(duration):
    img = np.zeros((Config.HEIGHT, Config.WIDTH, 4), dtype=np.uint8)
    top_alphas = (180 * (1 - np.arange(150) / 150)).astype(np.uint8)
    img[:150, :, 3] = top_alphas[:, np.newaxis]
    bottom_alphas = (180 * np.arange(400) / 400).astype(np.uint8)
    img[Config.HEIGHT - 400:, :, 3] = bottom_alphas[:, np.newaxis]
    return ImageClip(img, duration=duration)

def _create_silence(duration: float) -> AudioFileClip:
    fps = 44100
    arr = np.zeros((int(duration * fps), 2), dtype=np.float32)
    return AudioArrayClip(arr, fps=fps)

def compose_video(story, audio_clips_data, screenshot_paths, output_path, log_func):
    log_func("Compositing video layers...")
    clips_to_close = []
    try:
        audio_clips = []
        for i, d in enumerate(audio_clips_data):
            audio_clips.append(AudioFileClip(d["path"]))
            if i < len(audio_clips_data) - 1:
                audio_clips.append(_create_silence(Config.COMMENT_GAP))
        
        clips_to_close.extend(audio_clips)
        final_audio = concatenate_audioclips(audio_clips)
        narration_duration = final_audio.duration
        total_duration = Config.INTRO_DURATION + narration_duration + Config.OUTRO_DURATION

        if Config.BG_VIDEO and Path(Config.BG_VIDEO).exists():
            bg = VideoFileClip(Config.BG_VIDEO)
            clips_to_close.append(bg)
            if bg.duration < total_duration:
                bg = concatenate_videoclips([bg] * (int(total_duration / bg.duration) + 1))
            bg = bg.subclipped(0, total_duration).resized((Config.WIDTH, Config.HEIGHT))
            bg = bg.with_audio(None) if Config.BG_VIDEO_VOLUME == 0 else bg.with_volume_scaled(Config.BG_VIDEO_VOLUME)
        else:
            bg = ColorClip(size=(Config.WIDTH, Config.HEIGHT), color=(18, 18, 28), duration=total_duration)
        clips_to_close.append(bg)

        screenshot_clips = []
        current_time = Config.INTRO_DURATION
        
        for i, (data, path) in enumerate(zip(audio_clips_data, screenshot_paths)):
            pil_img = Image.open(path)
            sw, sh = pil_img.size
            scale = min(Config.SCREENSHOT_MAX_WIDTH / sw, Config.SCREENSHOT_MAX_HEIGHT / sh, 1.0)
            
            clip_duration = data["duration"] + (Config.COMMENT_GAP if i < len(audio_clips_data) - 1 else 0)
            if clip_duration <= 0:
                clip_duration = 0.1
            
            clip = ImageClip(str(path), duration=clip_duration)
            clip = clip.resized(lambda t: 1.0 + 0.08 * (t / clip_duration))
            clip = clip.resized((int(sw * scale), int(sh * scale)))
            
            clip = clip.with_position(("center", Config.SCREENSHOT_Y)).with_start(current_time)
            screenshot_clips.append(clip)
            clips_to_close.append(clip)
            
            current_time += clip_duration

        full_text = " ".join([d["text"] for d in audio_clips_data])
        all_timings = []
        time_offset = 0.0
        for i, d in enumerate(audio_clips_data):
            for t in d["timings"]:
                all_timings.append({**t, "offset": t["offset"] + time_offset})
            time_offset += d["duration"]
            if i < len(audio_clips_data) - 1:
                time_offset += Config.COMMENT_GAP

        text_clip = create_text_overlay_clip(full_text, all_timings, total_duration, Config.INTRO_DURATION)
        text_clip = text_clip.with_position(("center", Config.TEXT_Y))
        clips_to_close.append(text_clip)

        header = create_header_clip(story, total_duration).with_position(("center", Config.HEADER_Y))
        clips_to_close.append(header)

        final_audio = final_audio.with_start(Config.INTRO_DURATION)
        if Config.BG_MUSIC and Path(Config.BG_MUSIC).exists():
            music = AudioFileClip(Config.BG_MUSIC).with_volume_scaled(Config.BG_MUSIC_VOLUME)
            clips_to_close.append(music)
            if music.duration < total_duration:
                music = concatenate_audioclips([music] * (int(total_duration / music.duration) + 1))
            music = music.subclipped(0, total_duration)
            final_audio = CompositeAudioClip([final_audio, music])

        layers = [bg]
        if Config.DARKEN_BG:
            darken = create_darken_overlay(total_duration)
            clips_to_close.append(darken)
            layers.append(darken)
        layers.extend([*screenshot_clips, header, text_clip])
        if Config.WATERMARK:
            wm = create_watermark_clip(Config.WATERMARK, total_duration)
            clips_to_close.append(wm)
            layers.append(wm)

        final = CompositeVideoClip(layers, size=(Config.WIDTH, Config.HEIGHT))
        final = final.with_audio(final_audio)
        clips_to_close.append(final)

        if Config.FADE_DURATION > 0:
            try:
                final = final.with_effects([vfx.FadeIn(Config.FADE_DURATION), vfx.FadeOut(Config.FADE_DURATION)])
            except Exception as e:
                logging.warning(f"Fade effects failed: {e}")

        render_params = dict(
            fps=Config.FPS, codec=Config.VIDEO_CODEC, audio_codec=Config.AUDIO_CODEC,
            preset=Config.VIDEO_PRESET, bitrate=Config.VIDEO_BITRATE,
            threads=Config.THREADS,
            ffmpeg_params=["-pix_fmt", "yuv420p"], logger=None
        )

        if Config.MAX_PART_DURATION and total_duration > Config.MAX_PART_DURATION:
            parts = int(total_duration // Config.MAX_PART_DURATION) + 1
            for i in range(parts):
                start = i * Config.MAX_PART_DURATION
                end = min((i + 1) * Config.MAX_PART_DURATION, total_duration)
                part_clip = final.subclipped(start, end)
                part_path = output_path.parent / f"{output_path.stem}_part{i + 1}.mp4"
                log_func(f"Rendering Part {i + 1}/{parts}...")
                part_clip.write_videofile(str(part_path), **render_params)
                part_clip.close()
        else:
            log_func(f"Rendering full video (Using {Config.THREADS} CPU threads)...")
            final.write_videofile(str(output_path), **render_params)

        log_func(f"✅ Video saved to: {output_path}")
    finally:
        for clip in clips_to_close:
            try: clip.close()
            except: pass

# ═══════════════════════════════════════════════════════════════════════════════
# PYSIDE6 WORKERS
# ═══════════════════════════════════════════════════════════════════════════════

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLineEdit, QTextEdit, QPushButton, QLabel,
    QComboBox, QCheckBox, QFileDialog, QScrollArea, QGroupBox,
    QSpinBox, QDoubleSpinBox, QDialog, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QTabWidget, QProgressBar
)
from PySide6.QtGui import QShortcut, QKeySequence, QCloseEvent

class FetchWorker(QThread):
    finished = Signal(dict)
    error = Signal(str)
    log = Signal(str)

    def __init__(self, url: str, num_comments: int):
        super().__init__()
        self.url = url
        self.num_comments = num_comments

    def run(self):
        try:
            self.log.emit(f"Fetching URL: {self.url} (Comments: {self.num_comments})")
            story = fetch_reddit_post(self.url, self.num_comments)
            self.finished.emit(story)
            self.log.emit(f"✅ Fetched post and {len(story.get('comments', []))} comments successfully.")
        except Exception as e:
            self.error.emit(str(e))
            self.log.emit(f"❌ Error fetching URL: {e}")

class TestTTSWorker(QThread):
    finished = Signal()
    error = Signal(str)

    def __init__(self, voice, rate):
        super().__init__()
        self.voice = voice
        self.rate = rate

    def run(self):
        try:
            test_text = "This is a test of the selected voice. Hello there!"
            Config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
            path = Config.TEMP_DIR / "tts_test.mp3"
            generate_tts(test_text, path, self.voice, self.rate)
            open_in_os(str(path))
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))

class GenerateWorker(QThread):
    log = Signal(str)
    progress = Signal(int)
    finished = Signal(bool)

    def __init__(self, story: Dict, settings: Dict):
        super().__init__()
        self.story = story
        self.settings = settings
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
        self.log.emit("⚠️ Cancel requested. Will stop at next safe point...")

    def run(self):
        success = False
        try:
            self.progress.emit(0)
            apply_aspect_ratio(self.settings['aspect_ratio'])

            Config.TTS_VOICE = self.settings['voice']
            Config.COMMENT_VOICE = self.settings['comment_voice']
            Config.TTS_RATE = self.settings['rate']
            Config.BG_VIDEO = self.settings['bg_video']
            Config.BG_VIDEO_VOLUME = self.settings['bg_video_volume']
            Config.BG_MUSIC = self.settings['bg_music']
            Config.BG_MUSIC_VOLUME = self.settings['bg_music_volume']
            Config.WATERMARK = self.settings['watermark'] if self.settings['watermark'] else None
            Config.DARKEN_BG = self.settings['darken_bg']
            Config.MAX_PART_DURATION = 59 if self.settings['split'] else None
            Config.FADE_DURATION = self.settings.get('fade_duration', 0.5)
            Config.CLEANUP_TEMP = self.settings.get('cleanup_temp', True)

            Config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            Config.TEMP_DIR.mkdir(parents=True, exist_ok=True)

            self.log.emit(f"[1/4] Generating TTS & Screenshots ({Config.ASPECT_RATIO})...")
            self.progress.emit(10)
            audio_clips_data, screenshot_paths = [], []

            if self._cancelled: return

            post_text = sanitize_text_for_tts(f"{self.story['title']}. {self.story['body']}")
            post_audio_path = Config.TEMP_DIR / "narration_post.mp3"
            post_path, post_timings = generate_tts(post_text, post_audio_path, Config.TTS_VOICE, Config.TTS_RATE)
            
            post_audio = AudioFileClip(post_path)
            audio_clips_data.append({"path": post_path, "duration": post_audio.duration, "timings": post_timings, "text": post_text})
            post_audio.close()
            self.log.emit(f"  ✓ Post narration: {len(post_timings)} words")

            post_img_path = Config.TEMP_DIR / "reddit_post.png"
            create_reddit_screenshot(self.story, post_img_path)
            screenshot_paths.append(post_img_path)
            self.progress.emit(30)

            for i, comment in enumerate(self.story.get("comments", [])):
                if self._cancelled: return
                cmt_text = sanitize_text_for_tts(comment["body"])
                cmt_audio_path = Config.TEMP_DIR / f"narration_comment_{i}.mp3"
                cmt_path, cmt_timings = generate_tts(cmt_text, cmt_audio_path, Config.COMMENT_VOICE, Config.TTS_RATE)
                
                cmt_audio = AudioFileClip(cmt_path)
                audio_clips_data.append({"path": cmt_path, "duration": cmt_audio.duration, "timings": cmt_timings, "text": cmt_text})
                cmt_audio.close()
                self.log.emit(f"  ✓ Comment {i+1} narration: {len(cmt_timings)} words")

                cmt_img_path = Config.TEMP_DIR / f"reddit_comment_{i}.png"
                create_reddit_screenshot(comment, cmt_img_path, is_comment=True)
                screenshot_paths.append(cmt_img_path)
                self.progress.emit(30 + (i + 1) * 10)

            if self._cancelled: return
            self.log.emit("[2/4] Generating Subtitles (.srt)...")
            all_timings, offset = [], 0.0
            for i, d in enumerate(audio_clips_data):
                for t in d["timings"]:
                    all_timings.append({"word": t["word"], "offset": t["offset"] + offset, "duration": t["duration"]})
                offset += d["duration"]
                if i < len(audio_clips_data) - 1:
                    offset += Config.COMMENT_GAP
                    
            srt_path = Config.OUTPUT_DIR / f"reddit_{self.story['post_id']}.srt"
            generate_srt(all_timings, srt_path, Config.INTRO_DURATION)

            if self._cancelled: return
            self.log.emit("[3/4] Compositing Video Layers...")
            self.progress.emit(70)
            output_path = Config.OUTPUT_DIR / f"reddit_{self.story['post_id']}.mp4"
            compose_video(self.story, audio_clips_data, screenshot_paths, output_path, self.log.emit)

            self.log.emit(f"[4/4] Subtitles saved to: {srt_path}")
            self.log.emit("✅ Video generation finished successfully!")
            self.progress.emit(100)
            success = True

            if Config.CLEANUP_TEMP:
                try: shutil.rmtree(Config.TEMP_DIR)
                except: pass
        except Exception as e:
            self.log.emit(f"❌ FATAL ERROR: {e}")
            self.progress.emit(0)
            logging.exception("GenerateWorker fatal error")
        finally:
            self.finished.emit(success)

# ═══════════════════════════════════════════════════════════════════════════════
# GRAPHICAL USER INTERFACE
# ═══════════════════════════════════════════════════════════════════════════════

MODERN_DARK_STYLESHEET = """
QMainWindow, QWidget { background-color: #1e1e1e; color: #e0e0e0; font-family: 'Segoe UI', Arial, sans-serif; }
QGroupBox {
    border: 1px solid #3c3c3c; border-radius: 8px; margin-top: 16px;
    padding: 16px 12px 12px 12px; font-weight: bold; color: #ffffff;
    background-color: #252526;
}
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; background-color: #1e1e1e; }
QLineEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTableWidget {
    background-color: #2d2d30; border: 1px solid #3c3c3c; border-radius: 4px;
    padding: 6px; color: #ffffff; selection-background-color: #0078d4;
}
QLineEdit:focus, QTextEdit:focus { border: 1px solid #0078d4; }
QPushButton {
    background-color: #0078d4; border: none; padding: 8px 16px;
    border-radius: 4px; color: white; font-weight: bold;
}
QPushButton:hover { background-color: #1a8fe0; }
QPushButton:pressed { background-color: #005a9e; }
QPushButton:disabled { background-color: #3c3c3c; color: #7a7a7a; }
QPushButton#generateBtn { background-color: #107c10; }
QPushButton#generateBtn:hover { background-color: #13a013; }
QPushButton#generateBtn:disabled { background-color: #3c3c3c; }
QPushButton#cancelBtn { background-color: #c50f1f; }
QPushButton#cancelBtn:hover { background-color: #e02b3a; }
QPushButton#profileBtn { background-color: #5c2d91; }
QPushButton#profileBtn:hover { background-color: #6e3aab; }
QPushButton#testBtn { background-color: #008577; }
QPushButton#testBtn:hover { background-color: #00a392; }
QPushButton#clearBtn { background-color: #3c3c3c; }
QPushButton#clearBtn:hover { background-color: #505050; }
QPushButton#pasteBtn { background-color: #b8860b; }
QPushButton#pasteBtn:hover { background-color: #d49e1b; }
QPushButton#fetchBtn { background-color: #ff4500; }
QPushButton#fetchBtn:hover { background-color: #ff5a1f; }
QCheckBox { spacing: 8px; }
QScrollArea { border: none; }
QTextEdit#console {
    background-color: #1e1e1e; border: 1px solid #3c3c3c; color: #cccccc;
    font-family: Consolas, Monaco, monospace; font-size: 13px;
}
QProgressBar {
    border: 1px solid #3c3c3c; border-radius: 4px; text-align: center;
    background-color: #2d2d30; color: white; font-weight: bold;
}
QProgressBar::chunk { background-color: #107c10; border-radius: 3px; }
QLabel#hintLabel { color: #888888; font-size: 11px; font-style: italic; }
QLabel#countLabel { color: #aaaaaa; font-size: 11px; }
QHeaderView::section { background-color: #2d2d30; padding: 6px; border: 1px solid #3c3c3c; }
QTabWidget::pane { border: 1px solid #3c3c3c; border-radius: 4px; top: -1px; }
QTabBar::tab { background-color: #2d2d30; color: #aaaaaa; padding: 8px 16px; border: 1px solid #3c3c3c; border-bottom: none; border-top-left-radius: 4px; border-top-right-radius: 4px; }
QTabBar::tab:selected { background-color: #0078d4; color: white; }
QTabBar::tab:hover:!selected { background-color: #3c3c3c; }
"""

class CommentsDialog(QDialog):
    def __init__(self, comments: List[Dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Comments")
        self.resize(700, 400)
        self.comments = [c.copy() for c in comments]
        
        layout = QVBoxLayout(self)
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Author", "Upvotes", "Comment Body"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        
        btn_layout = QHBoxLayout()
        add_btn = QPushButton("Add Comment")
        add_btn.clicked.connect(self.add_comment)
        del_btn = QPushButton("Delete Selected")
        del_btn.clicked.connect(self.del_comment)
        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(del_btn)
        btn_layout.addStretch()
        
        ok_btn = QPushButton("Save & Close")
        ok_btn.setObjectName("generateBtn")
        ok_btn.clicked.connect(self.accept)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)
        self.populate_table()

    def populate_table(self):
        self.table.setRowCount(len(self.comments))
        for r, c in enumerate(self.comments):
            self.table.setItem(r, 0, QTableWidgetItem(c.get("author", "u/anonymous")))
            self.table.setItem(r, 1, QTableWidgetItem(c.get("upvotes", "0")))
            self.table.setItem(r, 2, QTableWidgetItem(c.get("body", "")))

    def add_comment(self):
        self.comments.append({"author": "u/new_commenter", "upvotes": "0", "body": ""})
        self.populate_table()
        self.table.scrollToBottom()

    def del_comment(self):
        r = self.table.currentRow()
        if r >= 0:
            del self.comments[r]
            self.populate_table()

    def get_comments(self) -> List[Dict]:
        result = []
        for r in range(self.table.rowCount()):
            # Safely fetch item text (prevents crash if a cell is completely empty)
            def get_text(col):
                item = self.table.item(r, col)
                return item.text() if item else ""
                
            result.append({
                "author": get_text(0),
                "upvotes": get_text(1),
                "body": get_text(2)
            })
        return result


class RedditVideoWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Reddit Video Studio Pro v6.1")
        self.resize(1280, 880)
        self.setStyleSheet(MODERN_DARK_STYLESHEET)

        self.fetch_worker = None
        self.gen_worker = None
        self.tts_worker = None
        self.comments_data = []
        
        # Pre-check dependencies
        self._missing_deps = check_python_dependencies()
        self._ffmpeg_ok = check_ffmpeg() if not self._missing_deps else False

        self.init_ui()
        self.setup_shortcuts()
        self.load_profile()
        
        if self._missing_deps:
            self.log_message("❌ FATAL: Missing Python dependencies.")
            self.gen_btn.setEnabled(False)
            self.gen_btn.setText("Dependencies Missing")
            QMessageBox.critical(self, "Missing Dependencies", 
                                f"The following packages are missing:\n{', '.join(self._missing_deps)}\n\n"
                                f"Please install them via pip:\npip install {' '.join(self._missing_deps)}")
        elif not self._ffmpeg_ok:
            self.log_message("❌ FATAL: FFmpeg is not installed or not in system PATH.")
            self.gen_btn.setEnabled(False)
            self.gen_btn.setText("FFmpeg Missing")
            QMessageBox.critical(self, "Dependency Missing", "FFmpeg is not installed or not in PATH.\n\nPlease install it to generate videos.")

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        top_layout = QHBoxLayout()

        # ── Left Panel (Tabbed) ───────────────────────────────────────────
        self.tabs = QTabWidget()
        self.tabs.setMaximumWidth(620)

        # ===== TAB 1: Story Editor =====
        story_tab = QWidget()
        story_layout = QVBoxLayout(story_tab)
        story_layout.setSpacing(15)

        gb_story = QGroupBox("Story Content (Copy & Paste or Type)")
        gb_layout = QVBoxLayout(gb_story)
        
        meta_form = QFormLayout()
        self.sub_input = QLineEdit("r/AskReddit")
        self.author_input = QLineEdit("u/anonymous")
        self.upvotes_input = QLineEdit("1.2k")
        meta_form.addRow("Subreddit:", self.sub_input)
        meta_form.addRow("Author:", self.author_input)
        meta_form.addRow("Upvotes:", self.upvotes_input)
        gb_layout.addLayout(meta_form)

        title_header = QHBoxLayout()
        title_header.addWidget(QLabel("Post Title:"))
        title_header.addStretch()
        self.title_count_lbl = QLabel("Words: 0")
        self.title_count_lbl.setObjectName("countLabel")
        title_header.addWidget(self.title_count_lbl)
        gb_layout.addLayout(title_header)
        
        self.title_input = QLineEdit()
        self.title_input.textChanged.connect(self.update_word_counts)
        gb_layout.addWidget(self.title_input)

        body_header = QHBoxLayout()
        body_header.addWidget(QLabel("Post Body:"))
        body_header.addStretch()
        self.body_count_lbl = QLabel("Words: 0")
        self.body_count_lbl.setObjectName("countLabel")
        body_header.addWidget(self.body_count_lbl)
        gb_layout.addLayout(body_header)

        self.body_input = QTextEdit()
        self.body_input.setMinimumHeight(150)
        self.body_input.textChanged.connect(self.update_word_counts)
        gb_layout.addWidget(self.body_input)

        paste_layout = QHBoxLayout()
        self.paste_title_btn = QPushButton("📋 Paste Title")
        self.paste_title_btn.setObjectName("pasteBtn")
        self.paste_title_btn.clicked.connect(lambda: self.paste_clipboard(self.title_input, is_line_edit=True))
        self.paste_body_btn = QPushButton("📋 Paste Body")
        self.paste_body_btn.setObjectName("pasteBtn")
        self.paste_body_btn.clicked.connect(lambda: self.paste_clipboard(self.body_input, is_line_edit=False))
        self.import_txt_btn = QPushButton("📁 Import .txt to Body")
        self.import_txt_btn.clicked.connect(self.import_text_file)
        
        paste_layout.addWidget(self.paste_title_btn)
        paste_layout.addWidget(self.paste_body_btn)
        paste_layout.addWidget(self.import_txt_btn)
        gb_layout.addLayout(paste_layout)

        cmt_layout = QHBoxLayout()
        cmt_layout.addWidget(QLabel("Comments:"))
        self.comment_count_lbl = QLabel("0 comments loaded")
        self.comment_count_lbl.setObjectName("countLabel")
        cmt_layout.addStretch()
        cmt_layout.addWidget(self.comment_count_lbl)
        self.edit_cmts_btn = QPushButton("✍️ Edit All Comments")
        self.edit_cmts_btn.setObjectName("profileBtn")
        self.edit_cmts_btn.clicked.connect(self.open_comments_dialog)
        cmt_layout.addWidget(self.edit_cmts_btn)
        gb_layout.addLayout(cmt_layout)
        
        story_layout.addWidget(gb_story)
        story_layout.addStretch()
        self.tabs.addTab(story_tab, "📝 Story Editor")

        # ===== TAB 2: Fetch from URL =====
        fetch_tab = QWidget()
        fetch_layout = QVBoxLayout(fetch_tab)
        
        gb_fetch = QGroupBox("Fetch from Reddit")
        fetch_gb_layout = QVBoxLayout(gb_fetch)
        
        url_row = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Paste Reddit URL here... (Ctrl+F)")
        self.fetch_btn = QPushButton("Fetch")
        self.fetch_btn.setObjectName("fetchBtn")
        self.fetch_btn.clicked.connect(self.start_fetch)
        url_row.addWidget(self.url_input)
        url_row.addWidget(self.fetch_btn)
        fetch_gb_layout.addLayout(url_row)

        cmt_row = QFormLayout()
        self.cmt_spin = QSpinBox()
        self.cmt_spin.setRange(0, 10)
        self.cmt_spin.setValue(2)
        cmt_row.addRow("Num Comments to Fetch:", self.cmt_spin)
        fetch_gb_layout.addLayout(cmt_row)

        hint_label = QLabel("Tip: Once fetched, the app will automatically switch to the Story Editor tab where you can edit the text.")
        hint_label.setObjectName("hintLabel")
        hint_label.setWordWrap(True)
        fetch_gb_layout.addWidget(hint_label)
        
        fetch_layout.addWidget(gb_fetch)
        fetch_layout.addStretch()
        self.tabs.addTab(fetch_tab, "🌐 Fetch from URL")

        # ===== TAB 3: Settings =====
        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)
        
        gb_settings = QGroupBox("Video & Audio Settings")
        s_layout = QVBoxLayout(gb_settings)

        self.aspect_input = QComboBox()
        self.aspect_input.addItems(["9:16 (Vertical/Shorts)", "16:9 (Horizontal/YouTube)"])

        self.voice_input = QComboBox()
        self.voice_input.addItems(["en-US-GuyNeural", "en-US-ChristopherNeural", "en-US-AriaNeural", "en-GB-RyanNeural", "en-AU-WilliamNeural"])
        self.comment_voice_input = QComboBox()
        self.comment_voice_input.addItems(["en-US-AriaNeural", "en-US-JennyNeural", "en-GB-SoniaNeural", "en-US-GuyNeural", "en-GB-RyanNeural"])

        self.rate_input = QComboBox()
        # Store actual TTS string data cleanly inside the combobox
        self.rate_input.addItem("Normal (+0%)", "+0%")
        self.rate_input.addItem("Fast (+15%)", "+15%")
        self.rate_input.addItem("Faster (+25%)", "+25%")
        self.rate_input.addItem("Fastest (+30%)", "+30%")
        self.rate_input.setCurrentIndex(1)

        gen_form = QFormLayout()
        gen_form.addRow("Aspect Ratio:", self.aspect_input)
        gen_form.addRow("Main Voice:", self.voice_input)
        gen_form.addRow("Comment Voice:", self.comment_voice_input)
        gen_form.addRow("TTS Speed:", self.rate_input)
        s_layout.addLayout(gen_form)

        test_layout = QHBoxLayout()
        self.test_voice_btn = QPushButton("▶ Test Main Voice")
        self.test_voice_btn.setObjectName("testBtn")
        self.test_voice_btn.clicked.connect(self.test_tts)
        test_layout.addWidget(self.test_voice_btn)
        self.test_cmt_voice_btn = QPushButton("▶ Test Comment Voice")
        self.test_cmt_voice_btn.setObjectName("testBtn")
        self.test_cmt_voice_btn.clicked.connect(self.test_tts_comment)
        test_layout.addWidget(self.test_cmt_voice_btn)
        s_layout.addLayout(test_layout)

        vid_layout = QHBoxLayout()
        self.vid_input = QLineEdit()
        self.vid_input.setPlaceholderText("Background Video (optional)")
        vid_btn = QPushButton("...")
        vid_btn.setFixedWidth(30)
        vid_btn.clicked.connect(lambda: self.pick_file(self.vid_input, "Video Files (*.mp4);;All Files (*)"))
        vid_layout.addWidget(self.vid_input)
        vid_layout.addWidget(vid_btn)
        s_layout.addWidget(QLabel("Background Video:"))
        s_layout.addLayout(vid_layout)

        self.vid_vol_spin = QDoubleSpinBox()
        self.vid_vol_spin.setRange(0.0, 1.0)
        self.vid_vol_spin.setSingleStep(0.05)
        self.vid_vol_spin.setValue(0.0)
        s_layout.addWidget(QLabel("Background Video Volume:"))
        s_layout.addWidget(self.vid_vol_spin)

        mus_layout = QHBoxLayout()
        self.mus_input = QLineEdit()
        self.mus_input.setPlaceholderText("Background Music (optional)")
        mus_btn = QPushButton("...")
        mus_btn.setFixedWidth(30)
        mus_btn.clicked.connect(lambda: self.pick_file(self.mus_input, "Audio Files (*.mp3);;All Files (*)"))
        mus_layout.addWidget(self.mus_input)
        mus_layout.addWidget(mus_btn)
        s_layout.addWidget(QLabel("Background Music:"))
        s_layout.addLayout(mus_layout)

        self.mus_vol_spin = QDoubleSpinBox()
        self.mus_vol_spin.setRange(0.0, 1.0)
        self.mus_vol_spin.setSingleStep(0.05)
        self.mus_vol_spin.setValue(0.15)
        s_layout.addWidget(QLabel("Background Music Volume:"))
        s_layout.addWidget(self.mus_vol_spin)

        self.watermark_input = QLineEdit()
        self.watermark_input.setPlaceholderText("@YourChannelName")
        wm_form = QFormLayout()
        wm_form.addRow("Watermark Text:", self.watermark_input)
        s_layout.addLayout(wm_form)

        self.split_check = QCheckBox("Auto-split into 60s Shorts")
        s_layout.addWidget(self.split_check)
        self.darken_check = QCheckBox("Darken Background (improves text readability)")
        self.darken_check.setChecked(True)
        s_layout.addWidget(self.darken_check)
        self.fade_check = QCheckBox("Fade In/Out Transitions")
        self.fade_check.setChecked(True)
        s_layout.addWidget(self.fade_check)
        self.cleanup_check = QCheckBox("Clean Temp Files After Generation")
        self.cleanup_check.setChecked(True)
        s_layout.addWidget(self.cleanup_check)
        self.auto_open_check = QCheckBox("Auto-Open Output Folder After Generation")
        s_layout.addWidget(self.auto_open_check)

        settings_layout.addWidget(gb_settings)
        settings_layout.addStretch()
        self.tabs.addTab(settings_tab, "⚙️ Settings")

        top_layout.addWidget(self.tabs)

        # ── Right Panel ───────────────────────────────────────────────────
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        
        # Create the console FIRST
        self.console = QTextEdit()
        self.console.setObjectName("console")
        self.console.setReadOnly(True)

        console_header = QHBoxLayout()
        console_header.addWidget(QLabel("Live Console Output"))
        console_header.addStretch()
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("clearBtn")
        self.clear_btn.setFixedWidth(60)
        self.clear_btn.clicked.connect(self.console.clear)
        console_header.addWidget(self.clear_btn)

        right_layout.addLayout(console_header)
        right_layout.addWidget(self.console, 1)
        top_layout.addWidget(right_panel, 1)

        main_layout.addLayout(top_layout, 1)

        bottom_layout = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Idle")
        
        action_layout = QHBoxLayout()
        self.preview_btn = QPushButton("Preview Images")
        self.preview_btn.clicked.connect(self.preview_images)
        self.gen_btn = QPushButton("Generate Video  (Ctrl+↵)")
        self.gen_btn.setObjectName("generateBtn")
        self.gen_btn.clicked.connect(self.start_generation)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("cancelBtn")
        self.cancel_btn.clicked.connect(self.cancel_generation)
        self.cancel_btn.setEnabled(False)
        
        self.save_profile_btn = QPushButton("Save Profile")
        self.save_profile_btn.setObjectName("profileBtn")
        self.save_profile_btn.clicked.connect(self.save_profile)
        self.open_folder_btn = QPushButton("Open Output")
        self.open_folder_btn.clicked.connect(self.open_output_folder)

        action_layout.addWidget(self.save_profile_btn)
        action_layout.addWidget(self.open_folder_btn)
        action_layout.addStretch()
        action_layout.addWidget(self.preview_btn)
        action_layout.addWidget(self.gen_btn)
        action_layout.addWidget(self.cancel_btn)

        bottom_layout.addWidget(QLabel("Progress:"))
        bottom_layout.addWidget(self.progress_bar, 1)
        bottom_layout.addLayout(action_layout)
        main_layout.addLayout(bottom_layout)

    def setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self.start_generation)
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(self.start_fetch)
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self.save_profile)
        QShortcut(QKeySequence("Ctrl+Shift+V"), self).activated.connect(lambda: self.paste_clipboard(self.body_input, is_line_edit=False))

    def closeEvent(self, event: QCloseEvent):
        """Safely terminate threads before closing the application."""
        workers = [self.fetch_worker, self.tts_worker, self.gen_worker]
        for worker in workers:
            if worker and worker.isRunning():
                if hasattr(worker, 'cancel'):
                    worker.cancel()
                worker.quit()
                worker.wait(2000)  # Wait up to 2 seconds for graceful shutdown
        event.accept()

    def update_word_counts(self):
        title_words = len(self.title_input.text().split())
        body_words = len(self.body_input.toPlainText().split())
        self.title_count_lbl.setText(f"Words: {title_words}")
        self.body_count_lbl.setText(f"Words: {body_words}")

    def paste_clipboard(self, target_widget, is_line_edit=False):
        clipboard = QApplication.clipboard()
        text = clipboard.text()
        if not text:
            QMessageBox.warning(self, "Clipboard Empty", "No text found in the clipboard.")
            return
        if is_line_edit:
            target_widget.setText(text)
        else:
            target_widget.setPlainText(text)
        self.log_message("✅ Text pasted from clipboard.")

    def import_text_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Import Text File", "", "Text Files (*.txt);;All Files (*)")
        if file_path:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    self.body_input.setPlainText(f.read())
                self.log_message(f"✅ Imported text from: {file_path}")
            except Exception as e:
                QMessageBox.critical(self, "Import Error", f"Failed to read file:\n{e}")

    def pick_file(self, target_line_edit, filter_str):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select File", "", filter_str)
        if file_path: target_line_edit.setText(file_path)

    def open_output_folder(self):
        Config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        open_in_os(str(Config.OUTPUT_DIR.resolve()))

    def log_message(self, msg):
        self.console.append(msg)
        self.console.verticalScrollBar().setValue(self.console.verticalScrollBar().maximum())
        logging.info(msg)

    def update_progress(self, val):
        self.progress_bar.setValue(val)
        if val == 0: self.progress_bar.setFormat("Idle")
        elif val < 100: self.progress_bar.setFormat("Processing... %p%")
        else: self.progress_bar.setFormat("Complete!")

    def save_profile(self):
        profile = {
            'aspect': self.aspect_input.currentText(), 'voice': self.voice_input.currentText(),
            'comment_voice': self.comment_voice_input.currentText(), 'rate_idx': self.rate_input.currentIndex(),
            'bg_video': self.vid_input.text(), 'bg_video_vol': self.vid_vol_spin.value(),
            'bg_music': self.mus_input.text(), 'bg_music_vol': self.mus_vol_spin.value(),
            'watermark': self.watermark_input.text(), 'split': self.split_check.isChecked(),
            'darken': self.darken_check.isChecked(), 'fade': self.fade_check.isChecked(),
            'cleanup': self.cleanup_check.isChecked(), 'auto_open': self.auto_open_check.isChecked(),
            'sub': self.sub_input.text(), 'author': self.author_input.text(), 'upvotes': self.upvotes_input.text(),
        }
        with open(Config.PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=4)
        self.log_message(f"✅ Profile saved to {Config.PROFILE_PATH}")

    def load_profile(self):
        if not Config.PROFILE_PATH.exists(): return
        try:
            with open(Config.PROFILE_PATH, "r", encoding="utf-8") as f: profile = json.load(f)
            self.aspect_input.setCurrentText(profile.get('aspect', '9:16 (Vertical/Shorts)'))
            self.voice_input.setCurrentText(profile.get('voice', 'en-US-GuyNeural'))
            self.comment_voice_input.setCurrentText(profile.get('comment_voice', 'en-US-AriaNeural'))
            
            # Backward compatibility for rate loading
            if 'rate_idx' in profile:
                self.rate_input.setCurrentIndex(profile.get('rate_idx', 1))
            else:
                self.rate_input.setCurrentIndex(1)
                
            self.vid_input.setText(profile.get('bg_video', ''))
            self.vid_vol_spin.setValue(profile.get('bg_video_vol', 0.0))
            self.mus_input.setText(profile.get('bg_music', ''))
            self.mus_vol_spin.setValue(profile.get('bg_music_vol', 0.15))
            self.watermark_input.setText(profile.get('watermark', ''))
            self.split_check.setChecked(profile.get('split', False))
            self.darken_check.setChecked(profile.get('darken', True))
            self.fade_check.setChecked(profile.get('fade', True))
            self.cleanup_check.setChecked(profile.get('cleanup', True))
            self.auto_open_check.setChecked(profile.get('auto_open', False))
            self.sub_input.setText(profile.get('sub', 'r/AskReddit'))
            self.author_input.setText(profile.get('author', 'u/anonymous'))
            self.upvotes_input.setText(profile.get('upvotes', '1.2k'))
            self.log_message("✅ Profile loaded successfully.")
        except Exception as e:
            self.log_message(f"⚠️ Error loading profile: {e}")

    def test_tts(self):
        self.test_voice_btn.setEnabled(False)
        self.test_voice_btn.setText("⏳ Generating...")
        raw_rate = self.rate_input.currentData()
        self.tts_worker = TestTTSWorker(self.voice_input.currentText(), raw_rate)
        self.tts_worker.finished.connect(lambda: self.reset_test_btn(self.test_voice_btn, "▶ Test Main Voice"))
        self.tts_worker.error.connect(lambda e: self.on_tts_test_error(e, self.test_voice_btn, "▶ Test Main Voice"))
        self.tts_worker.start()

    def test_tts_comment(self):
        self.test_cmt_voice_btn.setEnabled(False)
        self.test_cmt_voice_btn.setText("⏳ Generating...")
        raw_rate = self.rate_input.currentData()
        self.tts_worker = TestTTSWorker(self.comment_voice_input.currentText(), raw_rate)
        self.tts_worker.finished.connect(lambda: self.reset_test_btn(self.test_cmt_voice_btn, "▶ Test Comment Voice"))
        self.tts_worker.error.connect(lambda e: self.on_tts_test_error(e, self.test_cmt_voice_btn, "▶ Test Comment Voice"))
        self.tts_worker.start()

    def reset_test_btn(self, btn, text):
        btn.setEnabled(True)
        btn.setText(text)

    def on_tts_test_error(self, err, btn, text):
        self.reset_test_btn(btn, text)
        self.log_message(f"❌ TTS Test Error: {err}")

    def start_fetch(self):
        url = self.url_input.text().strip()
        if not url:
            self.log_message("⚠️ Please enter a URL first.")
            return
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("...")
        self.fetch_worker = FetchWorker(url, self.cmt_spin.value())
        self.fetch_worker.log.connect(self.log_message)
        self.fetch_worker.finished.connect(self.on_fetch_complete)
        self.fetch_worker.error.connect(self.log_message)
        self.fetch_worker.start()

    def on_fetch_complete(self, story):
        self.sub_input.setText(story.get("subreddit", "r/AskReddit"))
        self.author_input.setText(story.get("author", "u/anonymous"))
        self.upvotes_input.setText(story.get("upvotes", "0"))
        self.title_input.setText(story.get("title", ""))
        self.body_input.setPlainText(story.get("body", ""))
        
        self.comments_data = story.get("comments", [])
        self.comment_count_lbl.setText(f"{len(self.comments_data)} comments loaded")
            
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("Fetch")
        
        # Auto-switch to Story Editor tab to show fetched results
        self.tabs.setCurrentIndex(0)

    def open_comments_dialog(self):
        dialog = CommentsDialog(self.comments_data, self)
        if dialog.exec():
            self.comments_data = dialog.get_comments()
            self.comment_count_lbl.setText(f"{len(self.comments_data)} comments loaded")
            self.log_message(f"✅ Updated comments list ({len(self.comments_data)} total).")

    def get_story_data_from_ui(self) -> Dict:
        title = self.title_input.text().strip()
        body = self.body_input.toPlainText().strip()
        post_id = hashlib.md5(title.encode()).hexdigest()[:8]

        return {
            "title": title, "body": body,
            "subreddit": self.sub_input.text().strip(),
            "author": self.author_input.text().strip(),
            "upvotes": self.upvotes_input.text().strip(),
            "comment_count": len(self.comments_data),
            "post_id": post_id,
            "comments": self.comments_data,
        }

    def preview_images(self):
        title = self.title_input.text().strip()
        body = self.body_input.toPlainText().strip()
        if not title or not body:
            self.log_message("⚠️ Missing Data: Please enter at least a Title and Body.")
            return

        Config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        apply_aspect_ratio(self.aspect_input.currentText().split(" ")[0])
        story = self.get_story_data_from_ui()
        img_path = Config.TEMP_DIR / "preview_post.png"
        create_reddit_screenshot(story, img_path)
        self.log_message("Generated preview image.")

        open_in_os(str(img_path))

    def start_generation(self):
        if self._missing_deps or not self._ffmpeg_ok:
            QMessageBox.critical(self, "Error", "Cannot generate video. Dependencies or FFmpeg are missing.")
            return

        title = self.title_input.text().strip()
        body = self.body_input.toPlainText().strip()
        if not title or not body:
            self.log_message("⚠️ Missing Data: Title and Body cannot be empty.")
            return

        self.gen_btn.setEnabled(False)
        self.gen_btn.setText("Rendering...")
        self.preview_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.update_progress(0)

        story = self.get_story_data_from_ui()
        settings = {
            'aspect_ratio': self.aspect_input.currentText().split(" ")[0],
            'voice': self.voice_input.currentText(),
            'comment_voice': self.comment_voice_input.currentText(),
            'rate': self.rate_input.currentData(), # Pull clean string directly from item data
            'bg_video': self.vid_input.text().strip() or None,
            'bg_video_volume': self.vid_vol_spin.value(),
            'bg_music': self.mus_input.text().strip() or None,
            'bg_music_volume': self.mus_vol_spin.value(),
            'watermark': self.watermark_input.text().strip(),
            'darken_bg': self.darken_check.isChecked(),
            'split': self.split_check.isChecked(),
            'fade_duration': 0.5 if self.fade_check.isChecked() else 0.0,
            'cleanup_temp': self.cleanup_check.isChecked(),
        }

        Config.AUTO_OPEN_OUTPUT = self.auto_open_check.isChecked()

        self.gen_worker = GenerateWorker(story, settings)
        self.gen_worker.log.connect(self.log_message)
        self.gen_worker.progress.connect(self.update_progress)
        self.gen_worker.finished.connect(self.on_generation_finished)
        self.gen_worker.start()

    def cancel_generation(self):
        if self.gen_worker and self.gen_worker.isRunning():
            self.gen_worker.cancel()
            self.cancel_btn.setEnabled(False)

    def on_generation_finished(self, success):
        self.gen_btn.setEnabled(True)
        self.gen_btn.setText("Generate Video  (Ctrl+↵)")
        self.preview_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        if success and Config.AUTO_OPEN_OUTPUT:
            self.open_output_folder()

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log_file = setup_file_logging()
    logging.info("=" * 60)
    logging.info("Reddit Video Studio Pro - Session Started")
    
    missing = check_python_dependencies()
    if missing:
        print(f"ERROR: Missing dependencies: {', '.join(missing)}")
        print(f"Please run: pip install {' '.join(missing)}")
        sys.exit(1)

    app = QApplication(sys.argv)
    window = RedditVideoWindow()
    window.log_message(f"Session log: {log_file}")
    if window._ffmpeg_ok:
        window.log_message("✅ FFmpeg detected. Ready to render.")
    window.show()
    sys.exit(app.exec())
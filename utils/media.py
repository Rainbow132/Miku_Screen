# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import re
from datetime import timedelta
import time
import threading
from utils.logger import logger

# 引入 pygame 用于播放本地 FLAC/MP3 并获取精确时间
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
try:
    import pygame
except ImportError:
    pygame = None

HAS_WINSDK = False
try:
    import asyncio
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
    HAS_WINSDK = True
except ImportError:
    pass

class MediaAgent:
    """
    媒体捕获专员：
    - 支持通过 WinSDK 监听 Windows 系统级播放器（网易云、QQ音乐等）
    - 支持手动调用接口播放本地 FLAC/MP3，并自动解析同名 .lrc 歌词实时推送到屏幕
    - 播放完毕后自动退回系统监听模式
    """
    def __init__(self):
        self._current_song = ""
        self._local_elapsed = 0
        self._last_api_position_secs = 0
        self._lock = threading.RLock()
        self._mixer_ready = False

        # === 本地播放器特有状态 ===
        self.local_mode = False        # 是否处于本地歌曲播放模式
        self.local_song_title = ""     # 本地歌名
        self.local_lyrics = []         # 本地解析后的歌词列表 [(秒数, "歌词"), ...]
        self.local_duration = 0        # 本地歌曲总时长（秒）
        self.local_start_time = 0      # 记录本地播放开始的绝对时间戳
        self.local_pause_time = 0      # 记录暂停时的绝对时间戳
        self.local_is_playing = False  # 本地是否正在播放

    def _ensure_mixer(self):
        if pygame is None:
            logger.error("[Media] ????????? pygame ?")
            return False
        if self._mixer_ready and pygame.mixer.get_init():
            return True
        try:
            pygame.mixer.init()
            self._mixer_ready = True
            return True
        except Exception as exc:
            self._mixer_ready = False
            logger.error(f"[Media] ?????????: {exc}")
            return False

    def play_local_song(self, audio_path):
        """Play a local song and load a same-name .lrc file."""
        if not os.path.isabs(audio_path):
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            audio_path = os.path.join(base_dir, audio_path)

        logger.info(f"[Media] ????????????: {audio_path}")
        if not os.path.exists(audio_path):
            logger.error(f"[Media] ???????: {audio_path}")
            return

        with self._lock:
            if not self._ensure_mixer():
                return
            try:
                pygame.mixer.music.stop()
                sound = pygame.mixer.Sound(audio_path)
                local_duration = int(sound.get_length())
                pygame.mixer.music.load(audio_path)
                pygame.mixer.music.play()

                base_path, _ = os.path.splitext(audio_path)
                local_lyrics = self._parse_lrc(base_path + ".lrc")
                self.local_duration = local_duration
                self.local_is_playing = True
                self.local_start_time = time.time()
                self.local_pause_time = 0
                self.local_mode = True
                self.local_song_title = os.path.basename(base_path)
                self.local_lyrics = local_lyrics
                logger.info(
                    f"[Media] ???????: {self.local_song_title} "
                    f"(??: {self.local_duration}?, ????: {len(self.local_lyrics)})"
                )
            except Exception as exc:
                logger.error(f"[Media] ????????: {exc}")
                self.local_mode = False
                self.local_is_playing = False

    def stop_local_song(self):
        """Stop local playback and return to Windows media monitoring."""
        with self._lock:
            if self._mixer_ready and pygame and self.local_mode:
                try:
                    pygame.mixer.music.stop()
                except Exception as exc:
                    logger.warning(f"[Media] ???????????: {exc}")
            was_local = self.local_mode
            self.local_mode = False
            self.local_is_playing = False
            if was_local:
                logger.info("[Media] ??????????????????")

    def _parse_lrc(self, lrc_path):
        """
        解析标准 .lrc 歌词文件
        格式: [01:23.45] 歌词文本
        """
        lyrics = []
        if not os.path.exists(lrc_path):
            logger.warning(f"[Media] 未找到歌词文件: {lrc_path}，将只显示歌名")
            return lyrics

        pattern = re.compile(r'\[(\d+):(\d+(?:\.\d+)?)\](.*)')
        try:
            with open(lrc_path, 'r', encoding='utf-8') as f:
                for line in f:
                    match = pattern.match(line.strip())
                    if match:
                        minutes = int(match.group(1))
                        seconds = float(match.group(2))
                        text = match.group(3).strip()
                        total_seconds = minutes * 60 + seconds
                        lyrics.append((total_seconds, text))
            # 按照时间戳升序排序
            lyrics.sort(key=lambda x: x[0])
        except Exception as e:
            logger.error(f"[Media] 歌词文件解析失败: {e}")
        return lyrics

    def _get_current_lyric(self, elapsed_sec):
        """
        根据当前播放秒数，检索匹配对应的歌词行
        """
        if not self.local_lyrics:
            return self.local_song_title  # 没有歌词时，退回显示歌名
        
        current_lyric = ""
        for timestamp, text in self.local_lyrics:
            if elapsed_sec >= timestamp:
                current_lyric = text
            else:
                break
        return current_lyric if current_lyric else self.local_song_title

    def get_current_song_state(self):
        """
        原生系统监听逻辑（未开启本地模式时被调用）
        """
        if not HAS_WINSDK:
            return "晴天 - 周杰伦", True, timedelta(seconds=84), timedelta(seconds=242)
        try:
            async def _get_info():
                sessions = await MediaManager.request_async()
                current_session = sessions.get_current_session()
                if current_session:
                    info = await current_session.try_get_media_properties_async()
                    playback_info = current_session.get_playback_info()
                    
                    is_playing = (int(playback_info.playback_status) == 4) if playback_info and playback_info.playback_status is not None else False
                    
                    timeline = current_session.get_timeline_properties()
                    position_td = timeline.position
                    duration_td = timeline.end_time - timeline.start_time
                    
                    if info and info.title:
                        artist = info.artist if info.artist else "未知歌手"
                        return f"{info.title} - {artist}", is_playing, position_td, duration_td
                return "暂停播放", False, timedelta(0), timedelta(0)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            song, is_playing, pos_td, dur_td = loop.run_until_complete(_get_info())
            loop.close()
            return song, is_playing, pos_td, dur_td
        except Exception:
            return "暂无媒体", False, timedelta(0), timedelta(0)

    def format_time(self, current, total):
        current = max(0, current)
        total = max(0, total)
        current_m, current_s = divmod(int(current), 60)
        total_m, total_s = divmod(int(total), 60)
        return f"{current_m:02d}:{current_s:02d} / {total_m:02d}:{total_s:02d}"

    def update_and_get_media_info(self):
        with self._lock:
            if self.local_mode:
                if not self._mixer_ready or not pygame or not pygame.mixer.music.get_busy():
                    self.local_mode = False
                    self.local_is_playing = False
                    logger.info(f"[Media] ???? {self.local_song_title} ?????????????")
                else:
                    elapsed_ms = pygame.mixer.music.get_pos()
                    elapsed_float = max(0.0, elapsed_ms / 1000.0)
                    elapsed_float = min(float(self.local_duration), elapsed_float)
                    current_lyric = self._get_current_lyric(elapsed_float + 0.3)
                    elapsed = int(elapsed_float)
                    return {
                        "song_str_clean": current_lyric,
                        "music_time_str": self.format_time(elapsed, self.local_duration),
                        "percent": (elapsed / self.local_duration) if self.local_duration > 0 else 0,
                        "is_playing": self.local_is_playing,
                    }

        raw_song, is_playing, api_pos_td, api_dur_td = self.get_current_song_state()
        total_duration_sec = int(api_dur_td.total_seconds())
        song_display = f" {raw_song}" if is_playing else (
            "Paused" if raw_song == "????" else raw_song
        )
        if len(song_display) > 20:
            song_display = song_display[:20] + "..."

        api_pos_secs = int(api_pos_td.total_seconds())
        if raw_song != self._current_song and raw_song not in ["????", "????"]:
            self._current_song = raw_song
            self._local_elapsed = api_pos_secs
            self._last_api_position_secs = api_pos_secs
        elif abs(api_pos_secs - self._last_api_position_secs) > 2:
            self._local_elapsed = api_pos_secs
            self._last_api_position_secs = api_pos_secs
        else:
            if is_playing:
                self._local_elapsed += 1
            self._last_api_position_secs = api_pos_secs

        display_elapsed = min(total_duration_sec, self._local_elapsed + 1)
        return {
            "song_str_clean": song_display,
            "music_time_str": self.format_time(display_elapsed, total_duration_sec),
            "percent": (display_elapsed / total_duration_sec) if total_duration_sec > 0 else 0,
            "is_playing": is_playing,
        }

#!/usr/bin/env python
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later

from utils.sys_check import check_python_version
check_python_version()

import os
import signal
import subprocess
import sys
import time

from core.driver import LcdCommMatrixPlanet
from core.base_lcd import LcdComm, Orientation
from utils.logger import logger
from utils.config_store import ConfigStore

# 引入 ImageFilter 用于完美的高斯模糊图层合成
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# 引入 MVC 重构模块
from utils.telemetry import TelemetryManager
from utils.battery import is_case_battery_low
from core.renderer import (
    draw_transparent_icon,
    draw_music_progress_bar,
    draw_disk_progress_bar,
    draw_virtual_spectrum,
    draw_indicator_dot,
    draw_outlined_text,
    draw_tinted_icon 
)

# ==================== 配置区 ====================
COM_PORT = "COM5"
WIDTH, HEIGHT = 320, 480  # 原始竖屏分辨率
CONFIG_PATH = "config/layout_config.json"

# 静态背景路径
background = "res/backgrounds/background.png"
slider_dot_path = "res/icons/slider_dot.png"

# Periodically restart the process as a last-resort full reset.
FULL_REFRESH_INTERVAL = 7200.0
# Rebuild and resend one complete composited frame without restarting the process.
FRAME_RECOVERY_INTERVAL = 600.0

# 字体设置 (英数字体推荐使用您文件夹里的 Montserrat-Medium-7.otf)
CLOCK_FONT = "res/fonts/DS-DIGIB-2.ttf"
ENG_FONT = "res/fonts/Montserrat-Medium.otf"  # 👈 选定您截图中的 Medium 中粗体

# 中文字体自适应：默认使用下载的得意黑
CHN_FONT = "res/fonts/SmileySans-Oblique.ttf"
def get_valid_font(font_path):
    """
    检查字体是否存在，否则提供系统级后备字体防止程序崩溃
    """
    if os.path.exists(font_path):
        return font_path
    if "Montserrat-Medium" in font_path:
        fallback_light = font_path.replace("Montserrat-Medium", "Montserrat-Light")
        if os.path.exists(fallback_light):
            return fallback_light
            
    if os.name == 'nt':
        return "C:/Windows/Fonts/msyhbd.ttc"
    return "res/fonts/msyhbd.ttc"

CLOCK_FONT_PATH = get_valid_font(CLOCK_FONT)
ENG_FONT_PATH = get_valid_font(ENG_FONT)
CHN_FONT_PATH = get_valid_font(CHN_FONT)
# ================================================

stop = False

def clean_str(text, length=25):
    return text.ljust(length)


_clock_gradient_cache = {}


def get_clock_gradients(font_size):
    """Build the static neon gradients once per clock size."""
    key = int(font_size)
    cached = _clock_gradient_cache.get(key)
    if cached is not None:
        return cached

    grad_w, grad_h = 300, key + 4
    gradient_img = Image.new("RGBA", (grad_w, grad_h))
    white_core_img = Image.new("RGBA", (grad_w, grad_h))
    gradient_pixels = gradient_img.load()
    core_pixels = white_core_img.load()
    for y_pos in range(grad_h):
        for x_pos in range(grad_w):
            distance = x_pos + y_pos * 0.45
            if distance < 85:
                shell_color = (255, 45, 161, 255)
                core_color = (255, 242, 250, 255)
            elif distance < 125:
                blend = (distance - 85) / 40.0
                shell_color = (
                    int(255 + blend * (0 - 255)),
                    int(45 + blend * (230 - 45)),
                    int(161 + blend * (255 - 161)),
                    255,
                )
                core_color = (
                    int(255 + blend * (240 - 255)),
                    int(242 + blend * (255 - 242)),
                    int(250 + blend * (255 - 250)),
                    255,
                )
            else:
                shell_color = (0, 230, 255, 255)
                core_color = (240, 255, 255, 255)
            gradient_pixels[x_pos, y_pos] = shell_color
            core_pixels[x_pos, y_pos] = core_color

    cached = (gradient_img, white_core_img)
    _clock_gradient_cache[key] = cached
    return cached


def needs_redraw(cache, key, *values, force=False):
    fingerprint = tuple(values)
    if force or cache.get(key) != fingerprint:
        cache[key] = fingerprint
        return True
    return False

class MockLcd(LcdComm):
    """
    虚拟屏幕驱动类：用于拦截绘制，无痛合成第一帧高清调试图
    """
    def __init__(self, bg_path, width=480, height=320):
        super().__init__(com_port="MOCK", display_width=height, display_height=width)
        self.canvas = self.open_image(bg_path).convert("RGBA")
        self.orientation = 2  # 强制处于 Orientation.LANDSCAPE 横屏状态

    def DisplayPILImage(self, image, x, y, image_width=0, image_height=0):
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        self.canvas.paste(image, (x, y), image)

    def InitializeComm(self): pass
    def Reset(self): pass
    def Clear(self): pass
    def ScreenOff(self): pass
    def ScreenOn(self): pass
    def SetBrightness(self, level): pass
    def SetOrientation(self, orientation): self.orientation = orientation

if __name__ == "__main__":

    def sighandler(signum, frame):
        global stop
        stop = True

    signal.signal(signal.SIGINT, sighandler)
    signal.signal(signal.SIGTERM, sighandler)
    if os.name == 'posix':
        signal.signal(signal.SIGQUIT, sighandler)

    # 初始化屏幕
    assert WIDTH <= HEIGHT, "Indicate display width/height for PORTRAIT orientation"
    lcd_comm = LcdCommMatrixPlanet(com_port=COM_PORT, display_width=WIDTH, display_height=HEIGHT)
    physical_lcd = lcd_comm  # Keep the real serial device available during Mock capture.
    # Reset() already performs the MatrixPlanet handshake once.
    lcd_comm.Reset()
    lcd_comm.SetBrightness(level=30)
    lcd_comm.SetOrientation(orientation=Orientation.LANDSCAPE)  # 横屏 (480x320)

    if not os.path.exists(background):
        logger.error(f"未找到背景图：{background}")
        sys.exit(1)

    # 1. 刷入全屏幕背景底图
    lcd_comm.DisplayBitmap(background)

    # 读取初始城市配置并启动多线程
    default_ui = {
        "time_x": 8, "time_y": 12, "time_size": 64,
        "date_x": 20, "date_y": 82, "date_size": 13,
        "city": "北京市", "force_refresh": 0,
    }
    config_store = ConfigStore(CONFIG_PATH, default_ui)
    ui = config_store.get()
    config_city = ui.get("city", "北京市")

    telemetry = TelemetryManager(default_city=config_city)
    telemetry.start()

    logger.info("Miku 赛博监控系统（终极视觉版）正在运行...")
    
    # Treat the persisted refresh token as already consumed. The first frame
    # is forced separately, so startup must not trigger a second HID refresh.
    _last_force_refresh_id = ui.get("force_refresh", 0)
    _last_weather_state = ""
    _last_humidity_state = ""
    _last_indicator_status = -1
    _last_play_state = None  
    disk_counter = 60  

    cached_disk_str = ""
    cached_disk_percent = 0.0
    _has_saved_debug_frame = False

    # 👈 核心解耦：分别建立 图标 与 文字 的独立渲染缓存，彻底避免无意义的重绘
    _last_rendered_icons = {
        "kb_battery": None,
        "ms_battery": None,
        "gp_battery": None,
        "hp_battery": None
    }
    _render_cache = {}
    _last_rendered_texts = {
        "kb_battery": "",
        "ms_battery": "",
        "gp_battery": "",
        "hp_battery": ""
    }
    _last_render_force_id = -1  # 记录上一次强刷 ID

    def invalidate_render_cache():
        _render_cache.clear()
        for key in _last_rendered_icons:
            _last_rendered_icons[key] = None
        for key in _last_rendered_texts:
            _last_rendered_texts[key] = ""

    last_full_refresh = time.monotonic()
    last_frame_recovery = time.monotonic()
    restart_requested = False

    try:
        while not stop:
            start_loop = time.perf_counter()
        
            # --- 实时加载布局配置 (热重载) ---
            ui = config_store.get()
            current_city = ui.get("city", "北京市")
            telemetry.update_city(current_city)

            # Periodic full-frame recovery refresh (not the normal render loop).
            try:
                full_refresh_interval = float(ui.get("full_refresh_interval", FULL_REFRESH_INTERVAL))
            except (TypeError, ValueError):
                full_refresh_interval = FULL_REFRESH_INTERVAL
            full_refresh_interval = max(60.0, min(86400.0, full_refresh_interval))
            try:
                frame_recovery_interval = float(
                    ui.get("frame_recovery_interval", FRAME_RECOVERY_INTERVAL)
                )
            except (TypeError, ValueError):
                frame_recovery_interval = FRAME_RECOVERY_INTERVAL
            frame_recovery_interval = max(60.0, min(86400.0, frame_recovery_interval))

            current_refresh_id = ui.get("force_refresh", 0)
            config_refresh_requested = current_refresh_id != _last_force_refresh_id
            periodic_refresh_requested = (
                time.monotonic() - last_full_refresh >= full_refresh_interval
            )
            frame_recovery_requested = (
                time.monotonic() - last_frame_recovery >= frame_recovery_interval
            )

            if config_refresh_requested:
                _last_force_refresh_id = current_refresh_id

            if periodic_refresh_requested:
                # Restart the process after closing telemetry/COM5 in finally.
                logger.info("[recovery] periodic process restart requested")
                restart_requested = True
                break

            if config_refresh_requested:
                lcd_comm.DisplayBitmap(background)
                last_full_refresh = time.monotonic()
                _last_weather_state = ""
                _last_humidity_state = ""
                _last_indicator_status = -1
                _last_play_state = None
                disk_counter = 60
                telemetry.trigger_battery_instant_update()
                logger.info("layout force refresh applied")

            is_force_refresh = (
                config_refresh_requested
                or frame_recovery_requested
                or periodic_refresh_requested
                or current_refresh_id != _last_render_force_id
            )
            if is_force_refresh:
                _last_render_force_id = current_refresh_id
                invalidate_render_cache()

            snapshot = telemetry.get_snapshot()

            # ----------------------------------------------------
            # 首帧临时把物理屏幕句柄换成虚拟屏，拦截物理通信
            # ----------------------------------------------------
            original_lcd = lcd_comm
            render_full_frame = (
                not _has_saved_debug_frame or frame_recovery_requested
            )
            if render_full_frame:
                if not _has_saved_debug_frame:
                    logger.info("[调试] 正在等待后台线程抓取首帧真实数据...")
                    time.sleep(2.5)
                    snapshot = telemetry.get_snapshot()

                mock_lcd = MockLcd(background, width=480, height=320)
                lcd_comm = mock_lcd
            # ----------------------------------------------------

            # ==================== 控制器指挥渲染层推流 ====================
        
    # 1. 拟真气体发光氖灯管时钟渲染 (保留段落缝隙的同时呈现真实的 3D 体积感与斜向渐变)
            time_str = snapshot["time_str"]
            time_size = ui.get("time_size", 64)
            time_x = ui.get("time_x", 8)   
            time_y = ui.get("time_y", 12)  
            grad_w, grad_h = 300, time_size + 4

            try:
                # A. 创建高饱和度霓虹外壳的斜向渐变图
                gradient_img, white_core_img = get_clock_gradients(time_size)

                clock_font = lcd_comm.open_font(CLOCK_FONT_PATH, time_size)

                # ----------------- 遮罩图层渲染 -----------------
                # C-1. 核心电芯遮罩 (无描边 0px，作为高亮核心通道)
                core_mask = Image.new("L", (grad_w, grad_h), 0)
                core_draw = ImageDraw.Draw(core_mask)
                core_draw.text((0, 0), time_str, font=clock_font, fill=255)

                # C-2. 环境大气发光遮罩 (由 0px 笔划进行高斯模糊，模拟向背景发散的柔和霓虹氛围)
                ambient_glow = core_mask.filter(ImageFilter.GaussianBlur(radius=6.0))

                # C-3. 紧致霓虹灯壳遮罩 (2px 极细描边进行 1.5px 微幅模糊，维持字形高饱和轮廓)
                shell_raw_mask = Image.new("L", (grad_w, grad_h), 0)
                shell_raw_draw = ImageDraw.Draw(shell_raw_mask)
                shell_raw_draw.text((0, 0), time_str, font=clock_font, fill=255, stroke_width=2, stroke_fill=255)
                tight_glow = shell_raw_mask.filter(ImageFilter.GaussianBlur(radius=1.5))

                # ----------------- 物理图层多级叠合 -----------------
                bg = lcd_comm.open_image(background).convert("RGBA")
                bg_crop = bg.crop((time_x, time_y, time_x + grad_w, time_y + grad_h))

                # 1. 贴上大范围、低亮度的环境氛围光晕 (限制 30% 不透明度)
                ambient_glow_tint = ambient_glow.point(lambda p: int(p * 0.30))
                bg_crop.paste(gradient_img, (0, 0), ambient_glow_tint)

                # 2. 贴上紧密的高饱和玻璃彩色发光灯壳 (不透明度 100%)
                bg_crop.paste(gradient_img, (0, 0), tight_glow)

                # 3. 贴上高亮度电离白芯 (其坐落在灯管正中心，被两侧彩色边缘包裹，形成完美氖管厚度)
                bg_crop.paste(white_core_img, (0, 0), core_mask)

                # 裁剪底图并合成
                lcd_comm.DisplayPILImage(bg_crop.convert("RGB"), x=time_x, y=time_y, image_width=grad_w, image_height=grad_h)
            except Exception as e:
                logger.error(f"[Clock] 时钟双色渐变与拟真霓虹发光渲染失败: {e}")
            
            # 2. 日期（中英混合，降重 OPPO Sans 4.0）
            date_args = (snapshot["date_str"], ui.get("date_x", 20), ui.get("date_y", 82), ui.get("date_size", 13))
            if needs_redraw(_render_cache, "date", *date_args, force=is_force_refresh):
                lcd_comm.DisplayText(clean_str(snapshot["date_str"], 20), date_args[1], date_args[2],
                                     font=CHN_FONT_PATH, font_size=date_args[3],
                                     font_color=(255, 255, 255), background_image=background)

            # 3. 天气图标与天气字 (Montserrat-Medium 降重)
            weather_icon_path = f"res/icons/{snapshot['weather_icon']}"
            weather_icon_args = (snapshot["weather_icon"], ui.get("weather_icon_x", 20), ui.get("weather_icon_y", 115))
            if needs_redraw(_render_cache, "weather_icon", *weather_icon_args, force=is_force_refresh):
                if os.path.exists(weather_icon_path):
                    draw_transparent_icon(lcd_comm, background, weather_icon_path, weather_icon_args[1], weather_icon_args[2], 32, 32)
                _last_weather_state = snapshot["weather_icon"]
            weather_text_args = (snapshot["weather_temp"], ui.get("weather_text_x", 62), ui.get("weather_text_y", 121), ui.get("weather_text_size", 13))
            if needs_redraw(_render_cache, "weather_text", *weather_text_args, force=is_force_refresh):
                lcd_comm.DisplayText(clean_str(snapshot["weather_temp"], 15), weather_text_args[1], weather_text_args[2],
                                     font=ENG_FONT_PATH, font_size=weather_text_args[3], font_color=(255, 255, 255), background_image=background)

            # 4. 湿度图标与湿度字 (Montserrat-Medium 降重)
            humidity_icon_path = f"res/icons/{snapshot['humidity_icon']}"
            humidity_icon_args = (snapshot["humidity_icon"], ui.get("humidity_icon_x", 20), ui.get("humidity_icon_y", 155))
            if needs_redraw(_render_cache, "humidity_icon", *humidity_icon_args, force=is_force_refresh):
                if os.path.exists(humidity_icon_path):
                    draw_transparent_icon(lcd_comm, background, humidity_icon_path, humidity_icon_args[1], humidity_icon_args[2], 32, 32)
                _last_humidity_state = snapshot["humidity_icon"]
            humidity_text_args = (snapshot["humidity_val"], ui.get("humidity_text_x", 62), ui.get("humidity_text_y", 161), ui.get("humidity_text_size", 13))
            if needs_redraw(_render_cache, "humidity_text", *humidity_text_args, force=is_force_refresh):
                lcd_comm.DisplayText(clean_str(snapshot["humidity_val"], 15), humidity_text_args[1], humidity_text_args[2],
                                     font=ENG_FONT_PATH, font_size=humidity_text_args[3], font_color=(255, 255, 255), background_image=background)

            # 5. 底部防眩光极细歌名与播放状态描边绘制 (OPPO Sans 4.0 渲染中文歌名)
            song_args = (snapshot["song_str_clean"], ui.get("song_x", 50), ui.get("song_y", 265), ui.get("song_size", 11))
            if needs_redraw(_render_cache, "song", *song_args, force=is_force_refresh):
                draw_outlined_text(lcd_comm, background, clean_str(snapshot["song_str_clean"], 30),
                                   song_args[1], song_args[2], width=270, height=18,
                                   font_path=CHN_FONT_PATH, font_size=song_args[3],
                                   text_color=(0, 229, 255, 255), outline_color=(0, 0, 0, 255))

            # 6. Playback status
            is_playing = snapshot["is_playing"]
            play_args = (is_playing, ui.get("play_icon_x", 15), ui.get("play_icon_y", 252))
            if needs_redraw(_render_cache, "play_icon", *play_args, force=is_force_refresh):
                _last_play_state = is_playing
                state_icon_path = "res/icons/playing.png" if is_playing else "res/icons/paused.png"
                if os.path.exists(state_icon_path):
                    draw_transparent_icon(lcd_comm, background, state_icon_path, 
                                          play_args[1], play_args[2], 24, 24)

            # 7. 音乐时间轴 (Montserrat-Medium 渲染)
            music_time_args = (snapshot["music_time_str"], ui.get("music_time_x", 210), ui.get("music_time_y", 282), ui.get("music_time_size", 10))
            if needs_redraw(_render_cache, "music_time", *music_time_args, force=is_force_refresh):
                lcd_comm.DisplayText(snapshot["music_time_str"], music_time_args[1], music_time_args[2],
                                     font=ENG_FONT_PATH, font_size=music_time_args[3],
                                     font_color=(200, 200, 200), background_image=background)

            # 8. 音乐进度条 (renderer 内部已处理为 4px 轨道 and 8x8 圆点)
            progress_args = (round(snapshot["music_percent"], 4), ui.get("prog_bar_x", 15), ui.get("prog_bar_y", 288), ui.get("prog_bar_w", 270), ui.get("prog_bar_h", 12))
            if needs_redraw(_render_cache, "music_progress", *progress_args, force=is_force_refresh):
                draw_music_progress_bar(lcd_comm, background, slider_dot_path,
                                        x=progress_args[1], y=progress_args[2], width=progress_args[3], height=progress_args[4],
                                        percent=snapshot["music_percent"])

            # -------------------- 右半部分组件 --------------------
            # 9. 网速检测 (改用 CHN_FONT_PATH 粗体渲染，完美支持并显示精美的 ↑ 和 ↓ 箭头，且字形轮廓极清晰)
            up_args = (snapshot["up_speed"], ui.get("up_speed_x", 350), ui.get("up_speed_y", 58), ui.get("net_size", 11))
            if needs_redraw(_render_cache, "up_speed", *up_args, force=is_force_refresh):
                lcd_comm.DisplayText(clean_str(snapshot["up_speed"], 18), up_args[1], up_args[2],
                                     font=CHN_FONT_PATH, font_size=up_args[3], font_color=(0, 255, 200), background_image=background)
            down_args = (snapshot["down_speed"], ui.get("down_speed_x", 350), ui.get("down_speed_y", 78), ui.get("net_size", 11))
            if needs_redraw(_render_cache, "down_speed", *down_args, force=is_force_refresh):
                lcd_comm.DisplayText(clean_str(snapshot["down_speed"], 18), down_args[1], down_args[2],
                                     font=CHN_FONT_PATH, font_size=down_args[3], font_color=(0, 255, 200), background_image=background)

            # Disk values are sampled/displayed once per minute unless force-refreshed.
            disk_counter += 1
            if disk_counter >= 60 or is_force_refresh:
                disk_counter = 0
                cached_disk_str = snapshot["disk_str"]
                cached_disk_percent = snapshot["disk_percent"]
                # C 盘空间描述文字 (Montserrat-Medium 渲染)
                lcd_comm.DisplayText(clean_str(cached_disk_str, 25), ui.get("disk_x", 340), ui.get("disk_y", 98),
                                     font=ENG_FONT_PATH, font_size=ui.get("disk_size", 11),
                                     font_color=(255, 255, 255),
                                     background_image=background)
        
            # 磁盘进度条 (包含 136px 圆角 L-mask 裁剪与正弦波形线逻辑)
            draw_disk_progress_bar(lcd_comm, background, 
                                  x=ui.get("disk_bar_x", 350), y=ui.get("disk_bar_y", 125), 
                                  width=ui.get("disk_bar_w", 136), height=ui.get("disk_bar_h", 18), 
                                  percent=cached_disk_percent)

            # 11. 联网代理霓虹音符
            net_status = snapshot["net_status"]
            if _last_indicator_status != net_status or is_force_refresh:
                _last_indicator_status = net_status
                if net_status == 0:
                    net_icon_path = "res/icons/net_offline.png"
                elif net_status == 1:
                    net_icon_path = "res/icons/net_direct.png"
                else:
                    net_icon_path = "res/icons/net_proxy.png"
                
                if os.path.exists(net_icon_path):
                    draw_transparent_icon(lcd_comm, background, net_icon_path, 
                                          ui.get("net_icon_x", 336), ui.get("net_icon_y", 72), 32, 32)

    # ==================== 12. 外设电量：物理分离 + 独立解耦渲染 ====================
            peripherals = [
                {
                    "key": "kb_battery", 
                    "icon": "keyboard.png", 
                    "x_icon_key": "kb_icon_x", "y_icon_key": "kb_icon_y",
                    "x_text_key": "kb_text_x", "y_text_key": "kb_text_y"
                },
                {
                    "key": "ms_battery", 
                    "icon": "mouse.png", 
                    "x_icon_key": "ms_icon_x", "y_icon_key": "ms_icon_y",
                    "x_text_key": "ms_text_x", "y_text_key": "ms_text_y"
                },
                {
                    "key": "gp_battery", 
                    "icon": "controller.png", 
                    "x_icon_key": "gp_icon_x", "y_icon_key": "gp_icon_y",
                    "x_text_key": "gp_text_x", "y_text_key": "gp_text_y"
                },
                {
                    "key": "hp_battery", 
                    "icon": "headset.png", 
                    "x_icon_key": "hp_icon_x", "y_icon_key": "hp_icon_y",
                    "x_text_key": "hp_text_x", "y_text_key": "hp_text_y"
                },
            ]
        
            bat_size = ui.get("bat_size", 12)
        
            # 探测是否触发了全屏强制刷新
            for item in peripherals:
                raw_val = snapshot[item["key"]]
                is_charging = "+" in raw_val
                is_offline = raw_val == "OFF"
                # Headset icon indicates charging-case low battery, not charging.
                if item["key"] == "hp_battery":
                    icon_alert = is_case_battery_low(
                        snapshot.get("hp_case_battery", "OFF")
                    )
                else:
                    icon_alert = is_charging
            
                # A. 确定图标当前的物理颜色状态
                if is_offline:
                    icon_color = (110, 110, 110, 255)     # 中灰色图标（离线熄灭）
                    icon_state = "offline"
                elif icon_alert:
                    icon_color = (255, 0, 127, 255)      # 极光霓虹粉（高能充电）
                    icon_state = "low_battery" if item["key"] == "hp_battery" else "charging"
                else:
                    icon_color = (0, 229, 255, 255)      # 初音极光青（无线放电就绪）
                    icon_state = "online"
                
                # B. 确定显示文本的纯净字符串
                if is_offline:
                    display_text = "OFF"
                elif is_charging:
                    display_text = raw_val.replace("+", "").replace("%", "")
                else:
                    display_text = raw_val.replace("%", "")

                # -------------------- 解耦重绘支路 1：图标渲染 --------------------
                if icon_state != _last_rendered_icons[item["key"]] or is_force_refresh:
                    _last_rendered_icons[item["key"]] = icon_state
                
                    px_icon = ui.get(item["x_icon_key"])
                    py_icon = ui.get(item["y_icon_key"])
                
                    icon_path = f"res/icons/{item['icon']}"
                    if os.path.exists(icon_path):
                        draw_tinted_icon(lcd_comm, background, icon_path, 
                                         x=px_icon, y=py_icon, width=24, height=24, 
                                         target_color=icon_color)

                # -------------------- 解耦重绘支路 2：电量数值渲染 --------------------
                if display_text != _last_rendered_texts[item["key"]] or is_force_refresh:
                    _last_rendered_texts[item["key"]] = display_text
                
                    # 👈 核心改动：直接从 config 读取文字独立的坐标，完全摒弃任何硬编码加法运算
                    px_text = ui.get(item["x_text_key"])
                    py_text = ui.get(item["y_text_key"])
                
                    # 文字前景色映射：离线用中灰色，在线状态用高对比度纯白
                    text_color = (150, 150, 150) if is_offline else (255, 255, 255)
                
                    # 绘制电量文字，使用 ljust(3) 限制裁剪区域宽度
                    lcd_comm.DisplayText(display_text.ljust(3), 
                                         x=px_text, y=py_text,  # 👈 严格匹配 layout_config.json 中的独立文字坐标
                                         font=ENG_FONT_PATH, font_size=bat_size, 
                                         font_color=text_color, 
                                         background_image=background)

            # 13. 自适应虚拟音频频谱（自适应硬件负载判定模式 + 10 柱峰值保持算法）
            draw_virtual_spectrum(lcd_comm, background, 
                                  x=ui.get("spec_x", 350), y=ui.get("spec_y", 242), 
                                  width=ui.get("spec_w", 115), height=ui.get("spec_h", 40), 
                                  tick=time.time(),
                                  mode=snapshot["hardware_mode"])

            # ----------------------------------------------------
            # Compose the first pass off-screen, then send that exact complete
            # framebuffer to the physical LCD. This keeps the debug preview
            # while ensuring static regions are not lost during Mock takeover.
            # ----------------------------------------------------
            if render_full_frame:
                first_frame = mock_lcd.canvas.convert("RGB")
                if not _has_saved_debug_frame:
                    first_frame.save("debug_first_frame.png")

                # Restore the physical device before the full-frame transfer.
                lcd_comm = original_lcd
                lcd_comm.DisplayPILImage(
                    first_frame,
                    x=0,
                    y=0,
                    image_width=first_frame.width,
                    image_height=first_frame.height,
                )
                _has_saved_debug_frame = True
                if frame_recovery_requested:
                    last_frame_recovery = time.monotonic()
                    logger.info(
                        "[recovery] Complete composite frame resent to LCD"
                    )
                else:
                    logger.info(
                        "[debug] Complete first frame sent to LCD and saved as debug_first_frame.png"
                    )
            # ----------------------------------------------------
        
            # 维持 1 秒循环节律
            end_loop = time.perf_counter()
            sleep_time = max(0.1, 1.0 - (end_loop - start_loop))
            time.sleep(sleep_time)

    finally:
        telemetry.stop()
        physical_lcd.closeSerial()
        logger.info("????????????")
        if restart_requested:
            try:
                script_path = os.path.abspath(__file__)
                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.Popen(
                    [sys.executable, script_path],
                    cwd=os.path.dirname(script_path),
                    close_fds=True,
                    creationflags=creation_flags,
                )
                logger.info("[recovery] successor process started")
            except Exception as exc:
                logger.error("[recovery] successor process start failed: %s", exc)

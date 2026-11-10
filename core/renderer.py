# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import math
import time
from PIL import Image, ImageDraw, ImageFont
from utils.logger import logger

# 模块级全局变量：缓存 10 根频谱柱的历史最高高度
_spectrum_peaks = [0] * 10

def draw_transparent_icon(lcd, bg_path, icon_path, x, y, width=32, height=32):
    """
    将带有 Alpha 通道的透明图标与底图在内存中裁剪、合并，直接推流至 LCD。
    """
    try:
        bg = lcd.open_image(bg_path).convert("RGBA")
        bg_crop = bg.crop((x, y, x + width, y + height))
        
        icon = lcd.open_image(icon_path).convert("RGBA")
        if icon.size != (width, height):
            icon = icon.resize((width, height))
            
        bg_crop.paste(icon, (0, 0), icon)
        lcd.DisplayPILImage(bg_crop.convert("RGB"), x=x, y=y, image_width=width, image_height=height)
    except Exception as e:
        logger.error(f"[Renderer] 图标合成失败: {e}")

def draw_music_progress_bar(lcd, bg_path, dot_path, x, y, width, height, percent):
    """
    Pillow 内存合并音乐进度条：进度轨高度 4px，滑块缩减为 8x8 像素。
    """
    try:
        bg = lcd.open_image(bg_path).convert("RGBA")
        bg_crop = bg.crop((x, y, x + width, y + height))
        
        draw = ImageDraw.Draw(bg_crop)
        fill_len = int(width * percent)
        
        track_y = height // 2
        # 1. 绘制极细的暗灰色底盘轨道 (高度 4px)
        draw.line([(0, track_y), (width, track_y)], fill=(40, 40, 40, 255), width=4)
        
        if fill_len > 0:
            # 2. 绘制高亮霓虹青色已填充条
            draw.line([(0, track_y), (fill_len, track_y)], fill=(0, 255, 200, 255), width=4)
            
        # 3. 动态内存绘制发光滑块圆点 (完全无痛替代对外部 slider_dot.png 的依赖)
        dot_x = fill_len
        dot_x = max(4, min(width - 4, dot_x))  # 物理边界锁死限制，防止圆点滑出轨道外
        dot_y = height // 2
        
        # A. 绘制滑块外层冰青发光环 (半径 4px，直径 8px)
        draw.ellipse([dot_x - 4, dot_y - 4, dot_x + 4, dot_y + 4], fill=(0, 255, 200, 255))
        # B. 绘制滑块内层电离高亮白芯 (半径 1px，直径 3px，使其呈现极佳的 3D 立体感)
        draw.ellipse([dot_x - 1, dot_y - 1, dot_x + 1, dot_y + 1], fill=(255, 255, 255, 255))
            
        lcd.DisplayPILImage(bg_crop.convert("RGB"), x=x, y=y, image_width=width, image_height=height)
    except Exception as e:
        logger.error(f"[Renderer] 音乐进度条合成失败: {e}")

def draw_disk_progress_bar(lcd, bg_path, x, y, width, height, percent):
    """
    绘制 C 盘空间能量进度条：
    - 规格：宽度 136，高度 18，圆角 r = 9
    - 左侧已用空间：填充高度填满轨道的冰青至紫色渐变色块
    - 右侧未填充空白轨道：在剩下的空闲区域内绘制绿色 1px 正弦示波线，随 time.time() 缓慢流动
    - 采用 rounded_rectangle L-mask 进行完美裁剪，避免直角边缘溢出
    """
    try:
        bg = lcd.open_image(bg_path).convert("RGBA")
        bg_crop = bg.crop((x, y, x + width, y + height))
        
        # 创建透明叠加层，用于绘制动态元素
        bar_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(bar_overlay)
        
        fill_len = int(width * percent)
        
        # 1. 绘制左侧已用空间的冰青至霓虹紫色渐变
        if fill_len > 0:
            for gx in range(fill_len):
                t = gx / max(1, fill_len - 1)
                # 冰青 (0, 230, 255) -> 霓虹紫色 (159, 0, 255)
                r = int(0 + t * 159)
                g = int(230 + t * (0 - 230))
                b = int(255 + t * (255 - 255))
                overlay_draw.line([(gx, 0), (gx, height)], fill=(r, g, b, 255))
        
        # 2. 绘制右侧未填充轨道的绿色正弦波形线
        if fill_len < width:
            green_color = (0, 255, 0, 255)
            points = []
            for sx in range(fill_len, width):
                sy = height / 2.0 + 3.0 * math.sin(sx * 0.15 + time.time() * 2.0)
                points.append((sx, sy))
            if len(points) > 1:
                overlay_draw.line(points, fill=green_color, width=1)
            elif len(points) == 1:
                overlay_draw.point(points[0], fill=green_color)
        
        # 3. 创建圆角 L-mask 进行完美裁剪
        mask = Image.new("L", (width, height), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=9, fill=255)
        
        # 4. 合成至裁剪背景并推流至 LCD
        bg_crop.paste(bar_overlay, (0, 0), mask)
        lcd.DisplayPILImage(bg_crop.convert("RGB"), x=x, y=y, image_width=width, image_height=height)
    except Exception as e:
        logger.error(f"[Renderer] 磁盘进度条合成失败: {e}")

def draw_indicator_dot(lcd, bg_path, x, y, width=12, height=12, color=(0, 230, 255, 255)):
    """
    Pillow 内存合并：绘制网络/代理状态指示灯。
    """
    try:
        bg = lcd.open_image(bg_path).convert("RGBA")
        bg_crop = bg.crop((x, y, x + width, y + height))
        
        draw = ImageDraw.Draw(bg_crop)
        r = min(width, height) // 2
        cx, cy = width // 2, height // 2
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
        
        lcd.DisplayPILImage(bg_crop.convert("RGB"), x=x, y=y, image_width=width, image_height=height)
    except Exception as e:
        logger.error(f"[Renderer] 网络指示灯合成失败: {e}")

def draw_virtual_spectrum(lcd, bg_path, x, y, width, height, tick, mode="daily"):
    """
    Pillow 内存绘制：赛博霓虹虚拟音频频谱（自适应高度 ＋ 三色自适应比例渐变）
    """
    global _spectrum_peaks
    try:
        bg = lcd.open_image(bg_path).convert("RGBA")
        bg_crop = bg.crop((x, y, x + width, y + height))
        draw = ImageDraw.Draw(bg_crop)

        # 自适应硬件负载参数设定
        if mode == "game":
            speed = 2.4             # 极速波动
            max_height_ratio = 1.0  # 解锁 100% 满格高度上限
            density_shift = 0.9     # 相位起伏剧烈
        else:
            speed = 0.6             # 平缓低调呼吸律动
            max_height_ratio = 0.6  # 限制高度最高到 60% 能量
            density_shift = 0.5

        num_bars = 10
        bar_w = 10
        gap_x = 2
        
        # 👈 核心修复一：将最大波高与您在 GUI 中调整的物理高度（height）深度绑定！
        # 顶部预留 4 像素作为粉色峰值点的缓冲空间
        max_h = max(4, height - 4)  

        total_bars_w = (num_bars * bar_w) + ((num_bars - 1) * gap_x)
        offset_x = max(0, (width - total_bars_w) // 2)

        for i in range(num_bars):
            # 基础双重正弦波模拟
            wave1 = math.sin(tick * speed + i * density_shift)
            wave2 = math.cos(tick * (speed * 0.6) - i * (density_shift * 0.5))
            normalized_val = (wave1 + wave2 + 2.0) / 4.0  # 归一化至 [0, 1]
            normalized_val *= max_height_ratio

            # 计算当前实时柱高
            current_h = int(normalized_val * max_h)
            current_h = max(0, min(max_h, current_h))

            # 峰值保持与缓慢跌落逻辑
            if current_h >= _spectrum_peaks[i]:
                _spectrum_peaks[i] = current_h
            else:
                _spectrum_peaks[i] = max(0, _spectrum_peaks[i] - 1)

            x1 = offset_x + i * (bar_w + gap_x)
            x2 = x1 + bar_w - 1

            # 绘制当前波动柱体
            for y_pixel in range(current_h):
                y_curr = height - 1 - y_pixel
                if y_curr < 0:
                    continue
                
                # 👈 核心修复二：按比例动态划分三色渐变区间，不再写死 6 和 12 像素
                # 底部 40% 渲染冰青色，中间 40% 渲染深紫色，顶部 20% 渲染高能粉
                if y_pixel < int(max_h * 0.4):
                    color = (0, 230, 255, 255)   # Miku 冰青色
                elif y_pixel < int(max_h * 0.8):
                    color = (159, 0, 255, 255)  # 霓虹深紫色
                else:
                    color = (255, 0, 200, 255)  # 霓虹粉色
                draw.line([(x1, y_curr), (x2, y_curr)], fill=color)

            # 绘制 1px 高的粉色峰值小点
            if _spectrum_peaks[i] > 0:
                peak_y = height - 1 - _spectrum_peaks[i]
                if 0 <= peak_y < height:
                    draw.line([(x1, peak_y), (x2, peak_y)], fill=(255, 0, 200, 255))

        lcd.DisplayPILImage(bg_crop.convert("RGB"), x=x, y=y, image_width=width, image_height=height)
    except Exception as e:
        logger.error(f"[Renderer] 虚拟频谱绘制失败: {e}")

def draw_outlined_text(lcd, bg_path, text, x, y, width, height, font_path, font_size, text_color, outline_color=(0, 0, 0, 255)):
    """
    在内存中绘制带有 1px 黑色描边的文本，彻底解决发光粒子背景下的可读性问题，并单次推流至 LCD。
    """
    try:
        bg = lcd.open_image(bg_path).convert("RGBA")
        bg_crop = bg.crop((x, y, x + width, y + height))
        draw = ImageDraw.Draw(bg_crop)
        
        font = lcd.open_font(font_path, font_size)
        
        # 1px 8方向描边
        for dx, dy in [(-1, -1), (-1, 1), (1, -1), (1, 1), (0, -1), (0, 1), (-1, 0), (1, 0)]:
            draw.text((dx, dy), text, font=font, fill=outline_color)
            
        # 绘制主文本
        draw.text((0, 0), text, font=font, fill=text_color)
        
        lcd.DisplayPILImage(bg_crop.convert("RGB"), x=x, y=y, image_width=width, image_height=height)
    except Exception as e:
        logger.error(f"[Renderer] 描边文本绘制失败: {e}")
        
def draw_tinted_icon(lcd, bg_path, icon_path, x, y, width=24, height=24, target_color=(0, 229, 255, 255)):
    """
    单色透明图标动态着色函数：
    提取图标的 Alpha 通道，生成目标颜色的纯色块并进行形状剪裁，最后与底图合并推流至 LCD。
    """
    try:
        # 1. 裁剪对应的背景底图区域
        bg = lcd.open_image(bg_path).convert("RGBA")
        bg_crop = bg.crop((x, y, x + width, y + height))
        
        # 2. 打开图标并缩放至 24x24 黄金尺寸
        icon = lcd.open_image(icon_path).convert("RGBA")
        if icon.size != (width, height):
            icon = icon.resize((width, height), Image.Resampling.LANCZOS)
            
        # 3. 提取图标的 Alpha 通道作为形状剪裁遮罩 (不关心原图是黑是白，只提取轮廓)
        alpha_mask = icon.getchannel('A')
        
        # 4. 创建目标颜色的纯色块
        colored_block = Image.new("RGBA", (width, height), target_color)
        
        # 5. 将目标色块通过遮罩，完美贴合在背景图层上
        bg_crop.paste(colored_block, (0, 0), alpha_mask)
        
        # 6. 单次推流至物理/虚拟屏幕
        lcd.DisplayPILImage(bg_crop.convert("RGB"), x=x, y=y, image_width=width, image_height=height)
    except Exception as e:
        logger.error(f"[Renderer] 动态着色图标绘制失败: {e}")
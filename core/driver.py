# SPDX-License-Identifier: GPL-3.0-or-later
#
# turing-smart-screen-python - a Python system monitor and library for USB-C displays like Turing Smart Screen or XuanFang
# https://github.com/mathoudebine/turing-smart-screen-python/

import time
from typing import Optional
from PIL import Image

from core.base_lcd import *
from utils.logger import logger

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

def convert_to_rgb565_numpy(pil_img):
    """
    超高速 NumPy 大端序 RGB565 转换引擎 (耗时 1ms)
    """
    arr = np.array(pil_img, dtype=np.uint16)
    r = (arr[:, :, 0] >> 3).astype(np.uint16)
    g = (arr[:, :, 1] >> 2).astype(np.uint16)
    b = (arr[:, :, 2] >> 3).astype(np.uint16)
    rgb565 = (r << 11) | (g << 5) | b
    return rgb565.byteswap().tobytes()

def convert_to_rgb565_fallback(pil_img):
    from core.serialize import image_to_RGB565
    return image_to_RGB565(pil_img, "big")


class LcdCommMatrixPlanet(LcdComm):
    """
    针对 3.5寸 矩阵星球 (MatrixPlanet v3.5) 的原生适配驱动类
    采用 破译的 0x0258 查表法 CRC-16、自适应坐标旋转投影与 USB 64字节物理对齐
    """
    def __init__(self, com_port: str = "AUTO", display_width: int = 320, display_height: int = 480,
                 update_queue: Optional[queue.Queue] = None):
        logger.debug("HW revision: MatrixPlanet")
        
        # 选择转换引擎
        if HAS_NUMPY:
            self.convert_engine = convert_to_rgb565_numpy
            logger.info("[MatrixPlanet] 成功启用底层 NumPy 超高速图像加速引擎")
        else:
            self.convert_engine = convert_to_rgb565_fallback
            logger.warning("[MatrixPlanet] NumPy 未安装，降级为慢速 Fallback 引擎")

        # ================================================================
        # 【破译核心：预计算查找表】
        # 预先生成多项式为 0x0258 的 CRC-16 快速查找表
        # ================================================================
        self.crc_table = [0] * 256
        for i in range(256):
            crc = i << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = ((crc << 1) ^ 0x0258) & 0xFFFF
                else:
                    crc = (crc << 1) & 0xFFFF
            self.crc_table[i] = crc

        # 调用基类 LcdComm 的构造函数，基类会自动检测并打开串口
        LcdComm.__init__(self, com_port, display_width, display_height, update_queue)
        self.openSerial()

    def __del__(self):
        if hasattr(self, 'lcd_serial') and self.lcd_serial is not None:
            self.closeSerial()

    def _calc_matrix_crc16(self, data: bytes) -> int:
        """
        利用预计算查找表实现极速（几微秒）CRC-16 计算
        """
        crc = 0x0000
        for byte in data:
            idx = ((crc >> 8) ^ byte) & 0xFF
            crc = ((crc << 8) ^ self.crc_table[idx]) & 0xFFFF
        return crc

    def InitializeComm(self):
        """
        覆盖基类的初始化通信接口，执行握手并彻底解除硬件流控
        """
        logger.info("[MatrixPlanet] 正在初始化通信...")
        
        if self.lcd_serial:
            # Control-line states are configured before opening the port.
            # Reconfiguring an open USB serial handle can block in Windows.
            logger.info("[MatrixPlanet] Serial control lines configured before open; flushing input...")
            time.sleep(0.5)
            self.serial_flush_input()
        else:
            logger.warning("[MatrixPlanet] 基类串口实例 self.lcd_serial 尚未就绪...")

        # 发送挑战包
        c1 = bytearray(64)
        c1[0:6] = [0xfe, 0xef, 0x5f, 0x03, 0x9b, 0xa5]
        c2 = bytearray(64)
        c2[0:6] = [0xfe, 0xef, 0x08, 0x38, 0x78, 0xed]

        self.serial_write(bytes(c1))
        time.sleep(0.05)
        self.serial_write(bytes(c2))
        time.sleep(0.1)

        response = self.serial_read(8)

        if len(response) < 8:
            logger.warning("[MatrixPlanet] 默认极性未收到应答，正在切换 DTR/RTS 极性重试...")
            if self.lcd_serial:
                self.lcd_serial.dtr = True
                self.lcd_serial.rts = True
                time.sleep(0.2)
                self.serial_flush_input()
                self.serial_write(bytes(c1))
                time.sleep(0.05)
                self.serial_write(bytes(c2))
                time.sleep(0.1)
                response = self.serial_read(8)

        if len(response) >= 8 and response[0:2] == b"\xef\xfe":
            logger.info(f"[MatrixPlanet] 握手校验通过！硬件应答: {response.hex()}")
        else:
            logger.error(f"[MatrixPlanet] 硬件握手彻底失败，返回数据: {response.hex()}")

    def Reset(self):
        """重置显示器"""
        logger.info("Resets MatrixPlanet display...")
        self.InitializeComm()

    def Clear(self):
        """清屏"""
        black_img = Image.new("RGB", (320, 480), color=(0, 0, 0))
        self.DisplayPILImage(black_img)

    def ScreenOff(self):
        pass

    def ScreenOn(self):
        pass

    def SetBrightness(self, level: int = 25):
        pass

    def SetOrientation(self, orientation: Orientation = Orientation.PORTRAIT):
        self.orientation = orientation
        logger.debug(f"[MatrixPlanet] 设定屏幕方向为: {orientation}")

    def DisplayPILImage(
            self,
            image: Image.Image,
            x: int = 0, y: int = 0,
            image_width: int = 0,
            image_height: int = 0
        ):
        """
        核心下发接口：
        动态坐标映射投影、软件旋转、前 8 字节 Checksum、像素 0x0258 查表法 CRC-16 校验、物理 64 字节补齐
        """
        if not self.lcd_serial:
            logger.error("[MatrixPlanet] 找不到串口对象 self.lcd_serial，物理发送被拒绝！")
            return

        if not image_width:
            image_width = image.size[0]
        if not image_height:
            image_height = image.size[1]

        W_layout = self.get_width()
        H_layout = self.get_height()

        orientation_val = self.orientation.value if hasattr(self.orientation, 'value') else self.orientation

        # 1. 局部区域和排版坐标系动态投影到竖屏物理管道中
        if orientation_val == Orientation.LANDSCAPE:
            x_physical = y
            y_physical = W_layout - (x + image_width)
            w_physical = image_height
            h_physical = image_width
            send_img = image.transpose(Image.ROTATE_90)
            
        elif orientation_val == Orientation.REVERSE_LANDSCAPE:
            x_physical = H_layout - (y + image_height)
            y_physical = x
            w_physical = image_height
            h_physical = image_width
            send_img = image.transpose(Image.ROTATE_270)
            
        elif orientation_val == Orientation.REVERSE_PORTRAIT:
            x_physical = W_layout - (x + image_width)
            y_physical = H_layout - (y + image_height)
            w_physical = image_width
            h_physical = image_height
            send_img = image.transpose(Image.ROTATE_180)
            
        else:
            x_physical = x
            y_physical = y
            w_physical = image_width
            h_physical = image_height
            send_img = image

        # 2. 物理边界硬截断，防寄存器溢出导致 LCD 驱动芯片死锁
        x_physical = max(0, min(x_physical, 319))
        y_physical = max(0, min(y_physical, 479))
        w_physical = max(1, min(w_physical, 320 - x_physical))
        h_physical = max(1, min(h_physical, 480 - y_physical))

        # 3. 像素转换
        pixel_bytes = self.convert_engine(send_img)

        # 4. 构造配置帧的前 12 字节
        config = bytearray(64)
        config[0:12] = [
            (x_physical >> 8) & 0xFF, x_physical & 0xFF,
            (y_physical >> 8) & 0xFF, y_physical & 0xFF,
            (w_physical >> 8) & 0xFF, w_physical & 0xFF,
            (h_physical >> 8) & 0xFF, h_physical & 0xFF,
            0xfc, 0xfc,
            0x01, 0x00
        ]

        # 5. 动态计算前 8 字节的累计和作为第 12 和 13 字节 (破译算法一)
        header_sum = sum(config[0:8])
        config[12] = (header_sum >> 8) & 0xFF
        config[13] = header_sum & 0xFF

        # 6. 【重大破译：0x0258 极速查表 CRC 校验】
        # 将完整的 14 字节 Header 与 原始未补齐像素数据 拼接，计算 CRC-16
        header_14 = bytes(config[0:14])
        combined_data = header_14 + pixel_bytes
        crc_val = self._calc_matrix_crc16(combined_data)

        # 写入小端序 2 字节 CRC 到配置帧的第 14 和 15 字节 (破译算法二)
        config[14] = crc_val & 0xFF
        config[15] = (crc_val >> 8) & 0xFF

        # 7. 像素裸流 64 字节整倍数补齐 (供单片机 USB CDC 缓冲区无痛接收)
        padding_needed = (64 - (len(pixel_bytes) % 64)) % 64
        if padding_needed > 0:
            pixel_bytes += b'\x00' * padding_needed

        # 8. 绕过基类队列，直接写底层串口，毫秒级急速输出
        self.lcd_serial.write(bytes(config))
        self.lcd_serial.flush()
        time.sleep(0.005)
        self.lcd_serial.write(pixel_bytes)
        self.lcd_serial.flush()
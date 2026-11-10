# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later

from datetime import datetime, timedelta
from utils.weather import WeatherAgent
from utils.network import NetworkAgent
from utils.media import MediaAgent
from utils.system import SystemAgent
from utils.battery import BatteryAgent

# 👈 1. 引入标准库 HTTP 服务模块与多线程模块，实现 0 依赖 API 接口
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse
import threading
import json
import urllib.request

class TelemetryHTTPServer(HTTPServer):
    """自定义 HTTP 服务器，用于传递 telemetry 实例"""
    def __init__(self, server_address, RequestHandlerClass, telemetry_instance):
        self.telemetry = telemetry_instance
        super().__init__(server_address, RequestHandlerClass)

class TelemetryHTTPHandler(BaseHTTPRequestHandler):
    """API 请求处理 Handler"""
    def log_message(self, format, *args):
        # 覆写此方法以屏蔽终端里频繁打印的 HTTP 访问日志，保持监控控制台绝对纯净
        pass
        
    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        path = parsed_url.path
        
        # 拿到正在运行的 telemetry 实例
        telemetry = self.server.telemetry
        
        # 接口 A：播放本地歌曲 /play?song=music/...
        if path == "/play":
            song_path = query_params.get("song", [None])[0]
            if song_path:
                song_path = urllib.parse.unquote(song_path)  # 解密 URL 编码
                # 👈 核心：让当前正在运行的主程序实例去播放歌曲并切歌词状态！
                telemetry.media_agent.play_local_song(song_path)
                
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(f"正在播放本地音乐: {song_path}".encode('utf-8'))
                return
                
        # 接口 B：停止播放并恢复正常监听 /stop
        elif path == "/stop":
            telemetry.media_agent.stop_local_song()
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write("已停止本地播放，恢复普通系统媒体监听".encode('utf-8'))
            return
            
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")


class HeadsetStatusClient:
    """Read headset state from the independent HeadsetMonitor service."""

    def __init__(self, url="http://127.0.0.1:5010/status", timeout=0.25):
        self.url = url
        self.timeout = float(timeout)
        self._lock = threading.Lock()
        self._last_data = {"hp_battery": "OFF", "hp_case_battery": "OFF"}

    def get_data(self):
        try:
            with urllib.request.urlopen(self.url, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            data = {
                "hp_battery": payload.get("display_value", "OFF"),
                "hp_case_battery": payload.get("case_display_value", "OFF"),
            }
            with self._lock:
                self._last_data = data
            return data
        except Exception:
            with self._lock:
                return dict(self._last_data)


class TelemetryManager:
    """
    数据集成协调总线管理：采用 Facade 模式，向 main 隐藏所有锁与多线程采集逻辑
    """
    def __init__(self, default_city="北京市"):
        self.weather_agent = WeatherAgent(default_city)
        self.network_agent = NetworkAgent()
        self.media_agent = MediaAgent()
        self.system_agent = SystemAgent()
        
        self.headset_status_client = HeadsetStatusClient()
        if BatteryAgent:
            # Keyboard/mouse/controller collection stays local, but GX03 and
            # FxSound are owned by the independent HeadsetMonitor process.
            self.battery_agent = BatteryAgent(enable_headset=False)
        else:
            self.battery_agent = None

    def start(self):
        """启动后台常驻轮询线程"""
        self.weather_agent.start()
        self.network_agent.start()
        self.system_agent.start()
        if self.battery_agent:
            self.battery_agent.start()
            
        # 👈 2. 核心：启动本地 5000 端口极简 API 服务器
        try:
            self.http_server = TelemetryHTTPServer(('127.0.0.1', 5000), TelemetryHTTPHandler, self)
            self.http_thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
            self.http_thread.start()
            from utils.logger import logger
            logger.info("[HTTP] 本地控制 API 接口服务器已成功运行在 http://127.0.0.1:5000")
        except Exception as e:
            from utils.logger import logger
            logger.error(f"[HTTP] 本地 API 服务器启动失败（5000 端口可能被占用）: {e}")

    def stop(self):
        """优雅关闭常驻线程"""
        self.weather_agent.stop()
        self.network_agent.stop()
        self.system_agent.stop()
        if self.battery_agent:
            self.battery_agent.stop()
            
        # 👈 3. 退出时优雅关闭 HTTP API 服务器，释放端口
        if hasattr(self, 'http_server') and self.http_server:
            self.http_server.shutdown()
            self.http_server.server_close()
        if hasattr(self, 'http_thread') and self.http_thread.is_alive():
            self.http_thread.join(timeout=1.0)

    def update_city(self, new_city):
        """热更新目标采集城市"""
        self.weather_agent.update_city(new_city)

    def trigger_battery_instant_update(self):
        """触发外设电量即时主动更新"""
        if self.battery_agent:
            self.battery_agent.trigger_instant_update()

    def get_snapshot(self):
        """
        组装并返回纯净的数据快照
        """
        dt = datetime.now() + timedelta(seconds=1)
        time_str = dt.strftime("%H:%M:%S")
        
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        date_str = f"{dt.strftime('%m月%d日')} {weekdays[dt.weekday()]}"

        up_speed, down_speed = self.network_agent.get_speed_stacked()
        weather_data = self.weather_agent.get_data()
        media_data = self.media_agent.update_and_get_media_info()
        net_status = self.network_agent.get_net_status()
        disk_str, disk_percent = self.system_agent.get_disk_info()
        hardware_mode = self.system_agent.get_hardware_load_mode()

        # ==================== 🛡️ 核心防崩溃兜底设计 ====================
        # 预设默认电量数据，确保 kb_battery 等键永远存在，杜绝 KeyError
        battery_data = {
            "kb_battery": "OFF",
            "ms_battery": "OFF",
            "gp_battery": "OFF",
            "hp_battery": "OFF",
            "hp_case_battery": "OFF"
        }
        
        # 尝试更新物理电量数据
        try:
            if self.battery_agent:
                real_data = self.battery_agent.get_data()
                if isinstance(real_data, dict):
                    battery_data.update(real_data)
        except Exception:
            pass  # 发生任何异常皆安全降级，维持 OFF 状态，防闪退
        # The headset is owned by the independent monitor process. Keep its
        # status separate from keyboard/mouse/controller polling.
        try:
            headset_data = self.headset_status_client.get_data()
            if isinstance(headset_data, dict):
                battery_data.update(headset_data)
        except Exception:
            pass
        # ==============================================================

        return {
            "time_str": time_str,
            "date_str": date_str,
            "up_speed": up_speed,
            "down_speed": down_speed,
            "weather_temp": weather_data["temp"],
            "weather_icon": weather_data["weather_icon"],
            "humidity_val": weather_data["humidity_val"],
            "humidity_icon": weather_data["humidity_icon"],
            "song_str_clean": media_data["song_str_clean"],
            "music_time_str": media_data["music_time_str"],
            "music_percent": media_data["percent"],
            "is_playing": media_data["is_playing"],
            "net_status": net_status,
            "disk_str": disk_str,
            "disk_percent": disk_percent,
            "hardware_mode": hardware_mode,
            **battery_data  # 👈 确保解包合并
        }
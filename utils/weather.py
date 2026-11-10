# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later

import urllib.request
import urllib.parse
import json
import threading
import time
from utils.logger import logger

class WeatherAgent:
    """
    后台 15 分钟异步天气采集专员
    """
    def __init__(self, default_city="北京市"):
        self.city = default_city
        self.cache = {
            "temp": "26°C",
            "weather_icon": "weather_cloudy.png",
            "humidity_val": "45%",
            "humidity_icon": "humidity_normal.png"
        }
        self.lock = threading.Lock()
        self.stop_flag = False
        self.thread = None

    def start(self):
        self.stop_flag = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag = True
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)

    def update_city(self, new_city):
        with self.lock:
            if self.city != new_city:
                self.city = new_city

    def _loop(self):
        while not self.stop_flag:
            try:
                with self.lock:
                    city_name = self.city
                
                encoded_city = urllib.parse.quote(city_name)
                url = f"https://uapis.cn/api/v1/misc/weather?city={encoded_city}"
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                
                with urllib.request.urlopen(req, timeout=10) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    
                # 适配 uapis.cn 结构（无 code 键，直接判断 weather）
                if "weather" in data:
                    weather_desc = data.get("weather", "多云")
                    temp_val = data.get("temperature", 26)
                    humidity_val = data.get("humidity", 45)
                    
                    # 天气状态匹配图标
                    if "晴" in weather_desc:
                        w_icon = "weather_sunny.png"
                    elif "雨" in weather_desc:
                        w_icon = "weather_rainy.png"
                    elif "阴" in weather_desc:
                        w_icon = "weather_overcast.png"
                    elif "雪" in weather_desc:
                        w_icon = "weather_rainy.png"
                    else:
                        w_icon = "weather_cloudy.png"
                        
                    # 湿度匹配图标
                    if humidity_val < 40:
                        h_icon = "humidity_dry.png"
                    elif humidity_val <= 65:
                        h_icon = "humidity_normal.png"
                    else:
                        h_icon = "humidity_wet.png"
                        
                    with self.lock:
                        self.cache = {
                            "temp": f"{temp_val}°C",
                            "weather_icon": w_icon,
                            "humidity_val": f"{humidity_val}%",
                            "humidity_icon": h_icon
                        }
                    logger.info(f"[天气] 异步获取成功: {weather_desc} {temp_val}°C")
            except Exception as e:
                logger.error(f"[天气] 异步获取失败: {e}")
                
            # 15分钟休眠，支持秒级退出判定
            for _ in range(900):
                if self.stop_flag:
                    break
                time.sleep(1)

    def get_data(self):
        with self.lock:
            return self.cache.copy()
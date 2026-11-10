# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later

import urllib.request
import threading
import time
import psutil
from utils.logger import logger

class NetworkAgent:
    """
    后台 60 秒网络代理探针，以及物理网卡瞬时流量检测专员
    """
    def __init__(self):
        self.last_net_io = psutil.net_io_counters()
        self.last_time = time.perf_counter()
        self.net_status = 1  # 0: 断网, 1: 直连, 2: 代理
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

    def _check_internet_and_proxy(self):
        """双路极速 HTTP 状态检测"""
        try:
            # 强制重读最新的系统代理配置
            current_proxies = urllib.request.getproxies()
            
            # 直连探测
            direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            direct_opener.open("http://www.baidu.com", timeout=1.0)
            
            # 代理通道探测
            try:
                proxy_support = urllib.request.ProxyHandler(current_proxies)
                proxy_opener = urllib.request.build_opener(proxy_support)
                proxy_opener.open("http://www.google.com", timeout=1.0)
                return 2  # 代理连通
            except Exception:
                return 1  # 直连
        except Exception:
            return 0  # 离线

    def _loop(self):
        while not self.stop_flag:
            status = self._check_internet_and_proxy()
            with self.lock:
                self.net_status = status
            for _ in range(60):
                if self.stop_flag:
                    break
                time.sleep(1)

    def get_net_status(self):
        with self.lock:
            return self.net_status

    def get_speed_stacked(self):
        """实时计算一秒内的上传与下载网速"""
        now = time.perf_counter()
        dt = now - self.last_time
        if dt <= 0:
            return "↑ 0 B/s", "↓ 0 B/s"

        try:
            current_io = psutil.net_io_counters()
        except Exception:
            return "↑ 0 B/s", "↓ 0 B/s"

        bytes_recv = current_io.bytes_recv - self.last_net_io.bytes_recv
        bytes_sent = current_io.bytes_sent - self.last_net_io.bytes_sent

        self.last_net_io = current_io
        self.last_time = now

        def format_bytes(b_size):
            speed = b_size / dt
            if speed >= 1024 * 1024:
                return f"{speed / (1024 * 1024):.1f} MB/s"
            elif speed >= 1024:
                return f"{speed / 1024:.0f} KB/s"
            else:
                return f"{speed:.0f} B/s"

        return f"↑ {format_bytes(bytes_sent)}", f"↓ {format_bytes(bytes_recv)}"
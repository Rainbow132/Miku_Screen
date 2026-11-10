import tkinter as tk
from tkinter import ttk
import json
import os
import time

CONFIG_FILE = "config/layout_config.json"

# 默认配置，包含所有组件
ui_config = {
    "city": "北京市", 
    "time_x": 20, "time_y": 20, "time_size": 52,
    "date_x": 20, "date_y": 82, "date_size": 13,
    "weather_icon_x": 20, "weather_icon_y": 115, "weather_icon_size": 26,
    "weather_text_x": 62, "weather_text_y": 121, "weather_text_size": 13,
    "humidity_icon_x": 20, "humidity_icon_y": 155, "humidity_icon_size": 32,
    "humidity_text_x": 62, "humidity_text_y": 161, "humidity_text_size": 13,
    "song_x": 50, "song_y": 265, "song_size": 11,
    "music_time_x": 210, "music_time_y": 282, "music_time_size": 10,
    "up_speed_x": 350, "up_speed_y": 58, "net_size": 11,
    "down_speed_x": 350, "down_speed_y": 78,
    "disk_x": 340, "disk_y": 98, "disk_size": 11,
    
    # 👈 核心重构：将电量物理参数解耦，写入经过 410 中轴线严密计算的默认对齐坐标
    "kb_icon_x": 352, "kb_icon_y": 165,
    "kb_text_x": 380, "kb_text_y": 168,
    "ms_icon_x": 420, "ms_icon_y": 165,
    "ms_text_x": 448, "ms_text_y": 168,
    "gp_icon_x": 352, "gp_icon_y": 215,
    "gp_text_x": 380, "gp_text_y": 218,
    "hp_icon_x": 420, "hp_icon_y": 215,
    "hp_text_x": 448, "hp_text_y": 218,
    "bat_size": 12,
    
    "force_refresh": 0,
    
    # --- 组件配置 ---
    "spec_x": 350, "spec_y": 242, "spec_w": 115, "spec_h": 40,  
    "net_icon_x": 336, "net_icon_y": 72,     
    "play_icon_x": 15, "play_icon_y": 252,  
    "prog_bar_x": 15, "prog_bar_y": 288, "prog_bar_w": 270, "prog_bar_h": 12,
    "disk_bar_x": 350, "disk_bar_y": 125, "disk_bar_w": 115, "disk_bar_h": 8,
    "proxy_light_x": 336, "proxy_light_y": 72
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                ui_config.update(json.load(f))
        except Exception:
            pass

_save_after_id = None


def _collect_values():
    for key, var in vars_dict.items():
        ui_config[key] = var.get()


def _atomic_write_config():
    global _save_after_id
    _save_after_id = None
    os.makedirs(os.path.dirname(CONFIG_FILE) or ".", exist_ok=True)
    temp_file = CONFIG_FILE + ".tmp"
    with open(temp_file, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(ui_config, handle, indent=4, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_file, CONFIG_FILE)


def save_config(*args):
    global _save_after_id
    _collect_values()
    if _save_after_id is not None:
        root.after_cancel(_save_after_id)
    _save_after_id = root.after(150, _atomic_write_config)


def force_refresh_screen():
    global _save_after_id
    _collect_values()
    ui_config["force_refresh"] = time.time_ns()
    if _save_after_id is not None:
        root.after_cancel(_save_after_id)
        _save_after_id = None
    _atomic_write_config()


load_config()

root = tk.Tk()
root.title("Miku 监控屏布局编辑器")
root.geometry("450x700")
root.attributes("-topmost", True)

# 使用 Canvas 和 Scrollbar 实现界面滚动
canvas = tk.Canvas(root)
scrollbar = ttk.Scrollbar(root, orient="vertical", command=canvas.yview)
scrollable_frame = ttk.Frame(canvas)

scrollable_frame.bind(
    "<Configure>",
    lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
)

canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
canvas.configure(yscrollcommand=scrollbar.set)

canvas.pack(side="left", fill="both", expand=True)
scrollbar.pack(side="right", fill="y")


vars_dict = {}

def create_slider(parent, key, label_text, min_val, max_val):
    frame = ttk.Frame(parent)
    frame.pack(fill="x", padx=10, pady=2)
    
    ttk.Label(frame, text=label_text, width=15).pack(side="left")
    
    var = tk.IntVar(value=ui_config.get(key, 0))
    var.trace_add("write", save_config)
    vars_dict[key] = var
    
    # 底部滑块（粗调）
    slider = ttk.Scale(frame, from_=min_val, to=max_val, variable=var, orient="horizontal")
    slider.pack(side="left", fill="x", expand=True, padx=5)
    
    # 将单纯的 Label 替换为 Spinbox（输入框+上下箭头组件）
    # 支持点击箭头微调 (+-1) ，或者直接键盘输入数字
    spinbox = ttk.Spinbox(frame, from_=min_val, to=max_val, textvariable=var, width=5)
    spinbox.pack(side="left")

# 文本输入框函数
def create_text_input(parent, key, label_text):
    frame = ttk.Frame(parent)
    frame.pack(fill="x", padx=10, pady=2)
    
    ttk.Label(frame, text=label_text, width=15).pack(side="left")
    
    var = tk.StringVar(value=ui_config.get(key, ""))
    var.trace_add("write", save_config)
    vars_dict[key] = var
    
    entry = ttk.Entry(frame, textvariable=var)
    entry.pack(side="left", fill="x", expand=True, padx=5)

# 强制刷新按钮固定在最上面的一层
top_frame = ttk.Frame(root)
top_frame.place(relx=0.5, y=15, anchor="n") 
# 为了不挡住滚动条的内容，我们在 scrollable_frame 顶部加一个空白
ttk.Label(scrollable_frame, text="").pack(pady=15)
ttk.Button(top_frame, text="强制全屏刷新", command=force_refresh_screen).pack(pady=5)

# 注册所有组件
create_text_input(scrollable_frame, "city", "天气所在城市")  # 新增：把城市放在最上面的第一行
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

create_slider(scrollable_frame, "time_x", "时间 X", 0, 480)
create_slider(scrollable_frame, "time_y", "时间 Y", 0, 320)
create_slider(scrollable_frame, "time_size", "时间 字号", 10, 100)
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

create_slider(scrollable_frame, "date_x", "日期 X", 0, 480)
create_slider(scrollable_frame, "date_y", "日期 Y", 0, 320)
create_slider(scrollable_frame, "date_size", "日期 字号", 10, 50)
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

create_slider(scrollable_frame, "weather_icon_x", "天气图 X", 0, 480)
create_slider(scrollable_frame, "weather_icon_y", "天气图 Y", 0, 320)
create_slider(scrollable_frame, "weather_text_x", "天气字 X", 0, 480)
create_slider(scrollable_frame, "weather_text_y", "天气字 Y", 0, 320)
create_slider(scrollable_frame, "weather_text_size", "天气字号", 10, 50)  
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

create_slider(scrollable_frame, "humidity_icon_x", "湿度图 X", 0, 480)
create_slider(scrollable_frame, "humidity_icon_y", "湿度图 Y", 0, 320)
create_slider(scrollable_frame, "humidity_text_x", "湿度字 X", 0, 480)
create_slider(scrollable_frame, "humidity_text_y", "湿度字 Y", 0, 320)
create_slider(scrollable_frame, "humidity_text_size", "湿度字号", 10, 50)  
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

create_slider(scrollable_frame, "song_x", "歌曲名 X", 0, 480)
create_slider(scrollable_frame, "song_y", "歌曲名 Y", 0, 320)
create_slider(scrollable_frame, "song_size", "歌曲名字号", 10, 50)  

create_slider(scrollable_frame, "music_time_x", "歌曲时间 X", 0, 480)
create_slider(scrollable_frame, "music_time_y", "歌曲时间 Y", 0, 320)
create_slider(scrollable_frame, "music_time_size", "歌曲时间字号", 10, 50)  

create_slider(scrollable_frame, "play_icon_x", "播放图标 X", 0, 480)
create_slider(scrollable_frame, "play_icon_y", "播放图标 Y", 0, 320)
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

create_slider(scrollable_frame, "prog_bar_x", "音乐进度条 X", 0, 480)
create_slider(scrollable_frame, "prog_bar_y", "音乐进度条 Y", 0, 320)
create_slider(scrollable_frame, "prog_bar_w", "音乐进度条宽度", 10, 480)
create_slider(scrollable_frame, "prog_bar_h", "音乐进度条高度", 2, 50)
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

create_slider(scrollable_frame, "up_speed_x", "上传网速 X", 0, 480)
create_slider(scrollable_frame, "up_speed_y", "上传网速 Y", 0, 320)
create_slider(scrollable_frame, "down_speed_x", "下载网速 X", 0, 480)
create_slider(scrollable_frame, "down_speed_y", "下载网速 Y", 0, 320)
create_slider(scrollable_frame, "net_size", "网速字号", 10, 50)

create_slider(scrollable_frame, "net_icon_x", "网络音符 X", 0, 480)
create_slider(scrollable_frame, "net_icon_y", "网络音符 Y", 0, 320)
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

create_slider(scrollable_frame, "disk_x", "C盘空间 X", 0, 480)
create_slider(scrollable_frame, "disk_y", "C盘空间 Y", 0, 320)
create_slider(scrollable_frame, "disk_size", "C盘字号", 10, 50)

create_slider(scrollable_frame, "spec_x", "虚拟频谱 X", 0, 480)
create_slider(scrollable_frame, "spec_y", "虚拟频谱 Y", 0, 320)
create_slider(scrollable_frame, "spec_w", "虚拟频谱宽度", 10, 200)
create_slider(scrollable_frame, "spec_h", "虚拟频谱高度", 10, 100)
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

create_slider(scrollable_frame, "disk_bar_x", "C盘进度条 X", 0, 480)
create_slider(scrollable_frame, "disk_bar_y", "C盘进度条 Y", 0, 320)
create_slider(scrollable_frame, "disk_bar_w", "C盘进度条宽度", 10, 480)
create_slider(scrollable_frame, "disk_bar_h", "C盘进度条高度", 2, 50)
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

# ==================== 👈 核心重构：在注册区域生成独立的 图标 与 文字 滑块 ====================
create_slider(scrollable_frame, "bat_size", "全局电量字号", 10, 50)  
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

# 键盘独立控制
create_slider(scrollable_frame, "kb_icon_x", "键盘图标 X", 0, 480)
create_slider(scrollable_frame, "kb_icon_y", "键盘图标 Y", 0, 320)
create_slider(scrollable_frame, "kb_text_x", "键盘文字 X", 0, 480)
create_slider(scrollable_frame, "kb_text_y", "键盘文字 Y", 0, 320)
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

# 鼠标独立控制
create_slider(scrollable_frame, "ms_icon_x", "鼠标图标 X", 0, 480)
create_slider(scrollable_frame, "ms_icon_y", "鼠标图标 Y", 0, 320)
create_slider(scrollable_frame, "ms_text_x", "鼠标文字 X", 0, 480)
create_slider(scrollable_frame, "ms_text_y", "鼠标文字 Y", 0, 320)
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

# 手柄独立控制
create_slider(scrollable_frame, "gp_icon_x", "手柄图标 X", 0, 480)
create_slider(scrollable_frame, "gp_icon_y", "手柄图标 Y", 0, 320)
create_slider(scrollable_frame, "gp_text_x", "手柄文字 X", 0, 480)
create_slider(scrollable_frame, "gp_text_y", "手柄文字 Y", 0, 320)
ttk.Separator(scrollable_frame, orient="horizontal").pack(fill="x", pady=5)

# 耳机独立控制
create_slider(scrollable_frame, "hp_icon_x", "耳机图标 X", 0, 480)
create_slider(scrollable_frame, "hp_icon_y", "耳机图标 Y", 0, 320)
create_slider(scrollable_frame, "hp_text_x", "耳机文字 X", 0, 480)
create_slider(scrollable_frame, "hp_text_y", "耳机文字 Y", 0, 320)

save_config()
root.mainloop()
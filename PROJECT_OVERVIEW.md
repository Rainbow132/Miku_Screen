# 项目结构与运行说明

更新日期：2026-10-07（Asia/Shanghai）

## 项目用途

这是 Turing/MatrixPlanet 320×480 USB 串口副屏的 Python 监控程序。程序以横屏布局（逻辑画布 480×320）绘制背景、时钟、天气、媒体、网络、磁盘、频谱及外设电量，再经 COM5 写入屏幕控制器。

## 主要目录与文件

```text
Miku-Cyber-Screen/
├── main.py                     # 正式入口：初始化 LCD、配置、Telemetry，运行渲染循环
├── main_low_bandwidth_test.py  # 诊断脚本；不是正式入口，勿与 main.py 同时启动
├── gui_editor.py               # 布局配置编辑器
├── play_local.py               # 本地音乐播放/歌词相关入口；改动前先确认用户意图
├── config/
│   ├── layout_config.json      # 城市、布局、刷新间隔等运行配置，主循环热加载
│   └── layout_config.js*       # 编辑器/SignalRGB 侧使用的相关配置文件
├── core/
│   ├── base_lcd.py             # LCD 抽象层、图像/文本/进度条 API
│   ├── driver.py               # MatrixPlanet 设备驱动、COM5 握手、坐标旋转、帧头/校验/像素发送
│   ├── renderer.py             # 频谱、磁盘条、图标等渲染组件
│   ├── serialize.py            # 图像像素格式转换
│   └── color.py                # 颜色工具
├── utils/
│   ├── telemetry.py            # 采集器 Facade、HTTP 控制 API、数据快照
│   ├── battery.py              # 键盘/鼠标/手柄/GX03 电量及 HID 采集
│   ├── fxsound.py              # FxSound 输出设备/预设切换
│   ├── system.py               # 磁盘和硬件监控数据
│   ├── network.py              # 网络速度/连接状态
│   ├── media.py                # 媒体会话和本地播放状态
│   ├── weather.py              # 天气请求与缓存
│   ├── config_store.py         # mtime 热加载 JSON，保留最近一次有效配置
│   └── logger.py               # 日志设置
├── res/
│   ├── backgrounds/            # 副屏背景图；当前正式背景为 background.png
│   ├── fonts/                  # 字体
│   └── icons/                  # 天气、媒体、外设、网络图标
├── music/                      # 本地音乐资源
├── tests/                      # 电池/HID 与 FxSound 单元测试
├── log.log*                    # 滚动运行日志
└── backup*/、*.bak             # 历史备份/诊断版本，勿当作正式入口
```

## 主要数据与渲染流程

1. `main.py` 创建 `LcdCommMatrixPlanet(COM5)`，执行控制器握手、设置横屏方向，并发送背景。
2. 创建 `ConfigStore(config/layout_config.json)`；主循环会热读布局、城市、`force_refresh` 和 `full_refresh_interval`。
3. `TelemetryManager` 启动天气、网络、媒体、系统及电量后台采集，并提供每轮快照。
4. `main.py` 每秒运行渲染循环。静态/低频元素用渲染缓存避免重复写入；时钟、频谱、磁盘条等按各自代码路径更新。
5. MatrixPlanet 驱动将局部画面转换为 RGB565，构造设备帧并写 COM5。协议细节集中在 `core/driver.py`，属于设备私有协议。
6. 每轮结束 sleep 到目标循环节奏；退出时关闭 Telemetry 和串口。

## 当前正式配置（2026-10-07 检查）

- 串口：`COM5`，PnP 设备 `USB\VID_1A86&PID_5722\2017-2-25`，设备描述 MatrixPlanet Display。
- 逻辑画布：480×320 横屏；物理面板驱动尺寸 320×480。
- 城市：`广州市天河区`。
- `config/layout_config.json` 的 `full_refresh_interval` 当前为 **1800 秒（30 分钟）**。
- 正式主程序：`main.py`。
- 正式计划任务：`Miku Cyber Screen`，动作是 `F:\anaconda3\envs\tomato\pythonw.exe` 启动本目录 `main.py`，工作目录为本目录。
- 程序到期时会关闭 Telemetry/COM5，再用 `subprocess.Popen([sys.executable, __file__])` 启动继任进程。继任进程可能独立于计划任务存在，因此任务显示 Ready 不等于副屏没运行。

## 日志与诊断文件

- `log.log`：应用 logger 的滚动历史。
- `screen_runtime_stderr.log`：最近一次由手动/诊断脚本重启时重定向的标准错误；不一定对应当前继任进程。
- `debug_first_frame.png`：最近一次完整首帧的本地合成预览，不代表硬件当前 framebuffer 与它一致。
- `main_low_bandwidth_test.py`：低带宽诊断版本，关闭频谱、时钟降频、磁盘条降频；只用于对比实验，不用于正式启动。
- `rollback_before_revert_*`、`*.before-*.bak`：历史试验备份，不要自动选最新备份覆盖正式代码。

# Kunwei KWR75B Vendor GUI Ubuntu 可行性只读检查

## 实验目的

本报告判断 Kunwei KWR75B 厂家 Windows GUI 采集软件是否值得继续作为 Ubuntu/bench 采集路线验证。当前阶段只做只读检查：软件类型、Ubuntu 运行环境、教程参数、现有配置、当前网络可达性，以及下一步最小可行计划。

本报告不包含 live sensor 数据请求，不判断 Fz 噪声是否达到厂家声称的 0.01 N 级别。

## 设备与实验条件

| 项目 | 当前结果 |
|---|---|
| Vendor 软件目录 | `software/20260603` |
| 主程序 | `2. SensorLinker 网口 V3.0.23.39/SensorLinker.exe` |
| 软件类型 | Windows GUI PE32 `.NET/Mono` assembly |
| Ubuntu GUI 运行环境 | `wine` / `wine64` / `mono` / `dotnet` / `winetricks` / `bottles` / `flatpak` 均未安装 |
| Vendor 教程路线 | Ethernet / UDP |
| Sensor/server | `192.168.50.25:5152` |
| Local PC | `192.168.50.26:8886` |
| Decode | `28字节六维` |
| Model | `KWR75B(20KG)` |
| Demo collection mode | `主动获取（49）` |
| 当前 Ubuntu `enp3s0` | `192.168.1.10/24` |
| 当前到 sensor 的 route | `192.168.50.25 dev surfshark_wg table 300000 src 10.14.0.2` |
| Ping `192.168.50.25` | 2 packets transmitted, 0 received |

安全边界：没有移动 UR10e，没有运行 UR 程序，没有写 UR TCP/payload，没有发送 Kunwei zero/tare/filter/calibration/coefficient write，没有发送 UDP 5152 IP/port reconfiguration write，也没有发送 `49` 或其他 sensor-side 数据请求。

## 实验命令

主检查命令包括：

| 检查 | 命令 |
|---|---|
| EXE 类型 | `file SensorLinker.exe` |
| Windows runtime | `command -v wine wine64 mono dotnet winetricks bottles flatpak` |
| 当前网卡/路由 | `ip -brief addr`；`ip route get 192.168.50.25` |
| ICMP 可达性 | `ping -c 2 -W 1 192.168.50.25` |
| 教程文本 | `software/20260603/_analysis/software_connection_tutorial.txt` |
| GUI 配置 | `2. SensorLinker 网口 V3.0.23.39/AppConfig.json` |
| 视频元数据 | `ffprobe screenstudio/recording/channel-2-display-0.mp4` |

## 数据与图片

Vendor 教程视频 keyframe contact sheet 说明 GUI 参数路径、IP/port 设置和 UDP 工作流。它是当前只读阶段最重要的视觉证据。

![Vendor GUI 教程关键帧](../software/20260603/_analysis/video_keyframes/contact_sheet.jpg)

相关文件：

| 文件 | 用途 |
|---|---|
| `software/20260603/_analysis/software_connection_tutorial.txt` | PDF 教程提取文本 |
| `software/20260603/_analysis/video_keyframes/contact_sheet.jpg` | 视频教程关键帧总览 |
| `software/20260603/2. SensorLinker 网口 V3.0.23.39/AppConfig.json` | GUI 默认配置 |
| `software/20260603/screenstudio/recording/channel-2-display-0.mp4` | Vendor 操作录屏 |

## 统计结果

### Ubuntu 运行可行性

| 检查项 | 结果 | 判断 |
|---|---|---|
| `SensorLinker.exe` 类型 | `PE32 executable (GUI) Intel 80386 Mono/.Net assembly, for MS Windows` | 不能原生运行 |
| `wine` / `wine64` | 未安装 | 不能直接做 Wine 测试 |
| `mono` | 未安装 | 不能做 Mono 尝试 |
| `dotnet` | 未安装 | 不能做 .NET runtime 尝试 |
| `winetricks` / `bottles` / `flatpak` | 未安装 | 不能直接搭建 Wine/Bottles 环境 |
| WPF 风险 | 已知程序使用 WPF/PresentationFramework | 即使装 `dotnet`，也不等于可运行 Windows GUI |

### Vendor UDP 参数

| 参数 | 教程值 | 备注 |
|---|---|---|
| 通讯方式 | Ethernet / UDP | 不等同于 Ubuntu Python TCP streaming |
| Sensor/server | `192.168.50.25:5152` | 固定出厂参数 |
| Local PC | `192.168.50.26:8886` | 需要本机网卡配置 |
| 解码 | `28字节六维` | KWR75B-RS422 固定路线 |
| 传感器型号 | `KWR75B(20KG)` | GUI 解码选项 |
| Demo 采集方式 | `主动获取（49）` | live sensor 数据请求，执行前需要用户明确同意 |

`AppConfig.json` 当前保存的是 `192.168.1.25:5152` 和本地 `192.168.1.26:8886`，与本次 vendor 教程要求的 `192.168.50.25` / `192.168.50.26` 不一致。即使 GUI 能启动，也需要在 GUI 内改参数；这属于软件配置，不是 sensor IP/port reconfiguration write。

### 当前网络状态

| 检查项 | 结果 | 判断 |
|---|---|---|
| `enp3s0` | `192.168.1.10/24` | 不在 vendor UDP 所需 `192.168.50.26/24` |
| 到 `192.168.50.25` route | `surfshark_wg` | route 被 VPN 表接走，不是 bench 直连网口 |
| Ping | 100% packet loss | 当前不能验证 GUI 连接 sensor |

## 结论

当前 Ubuntu 不能直接运行 vendor GUI，也不能连接 sensor 做 live F/T 显示或导出验证。原因有两个独立阻塞：

1. Windows GUI runtime 未准备：Wine/Mono/.NET/Bottles/Flatpak 都不存在；`SensorLinker.exe` 是 Windows GUI PE32 `.NET/Mono` 程序。
2. Bench 网络未准备：本机不在 `192.168.50.26/24`，到 `192.168.50.25` 的路由走 VPN，ping 不通。

Vendor GUI 方案仍值得尝试，因为厂家教程明确提供 UDP 参数和 `主动获取（49）` 工作流，且用户关心厂家 GUI 是否包含自己的滤波/显示处理。但在没有 GUI 启动、没有 sensor 连接、没有数据导出前，不能把厂家 0.01 N 级别 Fz 误差当作本 bench 已验证结论。

## 下一步

最小可行执行计划：

| 步骤 | 动作 | 风险/审批 |
|---|---|---|
| 1 | 选择运行环境：优先 Windows laptop/VM；Ubuntu 路线才安装 Wine/Winetricks/.NET Framework runtime | 安装系统包或 VM 需要用户确认 |
| 2 | 恢复 bench 网络：让 Ubuntu 网口具有 `192.168.50.26/24`，并确保 `192.168.50.25` 不走 VPN route | 改网络配置需要用户确认 |
| 3 | 启动 GUI，只做界面启动验证，不连接 sensor | 低风险，取决于 GUI runtime |
| 4 | GUI 参数设为 Ethernet/UDP、server `192.168.50.25:5152`、local `192.168.50.26:8886`、decode `28字节六维`、model `KWR75B(20KG)`、collection `主动获取（49）` | GUI 参数配置，不写 sensor IP/port |
| 5 | 用户明确同意后点击连接/开始，允许 vendor GUI 发送 `49` 数据请求 | live sensor 数据请求，需要明确批准 |
| 6 | 若 GUI 显示或导出 F/T 数据，做短 no-motion sensor-only 采集，保存到 `measurements/vendor_gui_probe/` | 不移动 UR，不 zero/tare |
| 7 | 计算 Fz mean/std/range/drift，和 Ubuntu Python TCP route 分开报告 | 只比较指标，不混 route |

建议优先用 Windows laptop/VM 做第一轮 GUI 验证。Ubuntu Wine 路线可行但成本更高，且 WPF 兼容性不确定；只有当 Windows 环境不可用或必须在 Ubuntu bench 上运行时，才值得先安装 Wine/Winetricks/.NET runtime。

## 附录

如果进入 Ubuntu Wine 路线，下一步需要用户批准后再执行类似命令；这里仅列计划，不执行：

```bash
sudo apt update
sudo dpkg --add-architecture i386
sudo apt install wine64 wine32 winetricks
```

之后需要新建隔离 Wine prefix，并尝试安装 Windows .NET Framework runtime。具体版本应以实际 GUI 启动报错为准，不在只读阶段假定已经可行。

如果进入 live sensor 阶段，预期 sensor-side 数据请求是 vendor GUI 的 `主动获取（49）` 路线。未获用户明确同意前，不发送该请求。

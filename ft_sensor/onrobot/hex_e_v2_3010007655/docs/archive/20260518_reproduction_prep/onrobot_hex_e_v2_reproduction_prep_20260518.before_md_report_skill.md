# UR10e OnRobot HEX-E v2 复现准备报告

## 核心结论

这批照片记录的是借来的 OnRobot `HEX-E v2` 六轴力/力矩传感器 (force/torque sensor) 套件，用于 UR10e 上复现 TASE finite-time control 之前的硬件盘点、无运动 bring-up 和传感器测试准备。

从照片可以确认：

- 传感器型号：OnRobot `HEX-E v2`
- S/N：`3010007655`
- 套件标签：`HEX-E with Compute Box and Adapter flange A`
- box item：`100453`
- Compute Box 标注固定 IP：`192.168.1.1`
- 第一阶段数据通路：先走 `Compute Box -> 电脑`，不先依赖 URCap/PolyScope。

OnRobot 当前定位是“临时验证平台”：先用它把 UR10e、打印末端、force logging、实验 protocol 跑顺。根据 PBT group 售前反馈，Robotiq 很可能是长期更省事的方案，因为它和 UR 的集成更直接；但对 TASE 来说，最终还是要验证数据频率、latency、frame 定义和 logging 稳定性。

## 传感器策略

OnRobot 不应该变成整个实验架构的绑定前提。现在用它验证通用流程：

- 无运动读取外部 wrench data；
- 建立零漂 (zero drift)、温漂 (thermal drift)、noise floor、repeatability 和 warm-up baseline；
- 确认当前打印末端、UR10e、光学平台/接触环境之间的几何关系；
- 形成以后 Robotiq 或坤维也必须通过的 vendor-agnostic 测试清单。

Robotiq 要作为长期易用候选重点看：

- UR 侧如何暴露 force/torque data；
- 采样率/抖动 (sample rate/jitter) 是否足够稳定；
- 数据链路是否能支撑 finite-time surface-contact control；
- 集成便利性是否真的胜过 raw data 可控性。

坤维仍保留为 PC-side data access 或成本路线的备选，但测试 protocol 不应该绑定某一家 vendor 的软件栈。

## 逐张照片盘点

| 文件 | 内容 | 用途 | 备注 |
|---|---|---|---|
| `IMG_1166.HEIC` | 箱盖 packout diagram，列出 Compute Box、pen drive、HEX sensor、adapter/screw set、screw tool、PG16 cable gland、24V adapter/cable、UTP cable、sensor cable。 | 文档证据；立刻可用作 inventory checklist。 | 无明显不确定。 |
| `IMG_1167.HEIC` | Calibration certificate，S/N `3010007655`，manufacturing ID `HEXEB306`，factory calibration date `2021-09-06`。 | 关键 traceability 证据。 | 证书只能说明出厂校准；本地仍要重新测零漂、温漂和 noise。 |
| `IMG_1168.HEIC` | Packing slip，日期 `2022-11-15`，内容偏 repair/warranty。 | 文档证据；对当前 bring-up 不是关键。 | 看起来更像历史 RG2 repair/warranty 文件。 |
| `IMG_1169.HEIC` | Main packing slip，`HEX-E with Compute Box and Adapter flange A`，列出多个 serial，其中包括 `3010007655`。 | 套件身份和借用追踪证据。 | 当前实物身份以 sensor label 为准。 |
| `IMG_1170.HEIC` | 箱体规格标签，确认 HEX-E、Adapter flange A、Compute Box、item `100453`、S/N `3010007655`。 | 关键型号/配置确认。 | 无明显不确定。 |
| `IMG_1171.HEIC` | UR10e bench 总览，能看到当前打印末端、机器人和实验台环境。 | TASE 复现实验现场 context。 | 不能证明 force sensor wiring。 |
| `IMG_1172.HEIC` | 当前打印末端靠近光学平台/接触区域的近景。 | 接触几何、clearance、末端形状 context。 | 不能直接确认最终 TCP 或 contact frame。 |
| `IMG_1173.HEIC` | 24V XP Power adapter，输出 `24V 1.0A`。 | Compute Box bring-up 立即有用。 | 需要确认本地插头/插座兼容。 |
| `IMG_1174.HEIC` | 24V power adapter safety leaflet。 | 文档证据；短期技术价值较低。 | 保留在套件档案中即可。 |
| `IMG_1175.HEIC` | 开箱总览，包含 sensor、Compute Box、USB drive、bagged accessories、cable 和 foam layout。 | inventory、repacking、missing-part check。 | 小附件细节不完全可见。 |
| `IMG_1176.HEIC` | Adapter flange A 一面，含 circular register 和 bolt holes。 | 安装兼容性检查。 | 具体哪面朝 robot/tool 要查 manual。 |
| `IMG_1178.HEIC` | Adapter flange A 另一面，含 recess 和 bolt pattern。 | adapter 方向和装配检查。 | screw assignment 需要 manual 确认。 |
| `IMG_1179.HEIC` | HEX-E 黑色保护面/top-bottom 视角。 | sensor orientation evidence。 | sensor frame 方向需要 manual 确认。 |
| `IMG_1180.HEIC` | HEX-E 侧面，看到 connector 和黑色保护面。 | cable routing、strain relief。 | 与相邻照片有重复价值。 |
| `IMG_1181.HEIC` | HEX-E 侧面，看到 OnRobot logo 和 connector 位置。 | orientation evidence。 | 与 `IMG_1180` 接近，但仍保留。 |
| `IMG_1182.HEIC` | model/serial label：`HEX-E v2`、`100453/IP67`、S/N `3010007655`。 | 关键身份照片。 | 无明显不确定。 |
| `IMG_1183.HEIC` | mounting face，含 circular interface、O-ring、threaded holes、OnRobot quick interface feature。 | 安装方向和 interface 判断。 | robot-side/tool-side 方向要查 manual。 |
| `IMG_1184.HEIC` | 袋装银色 accessory hardware。 | 安装相关资产。 | 具体是 screws/tool/cable-holder 中哪些件待确认。 |
| `IMG_1185.HEIC` | 袋装黑色 accessory pieces。 | 安装相关资产。 | 每个黑色件具体功能待确认。 |
| `IMG_1186.HEIC` | 黑色 accessory pieces 的另一个角度。 | inventory evidence。 | 基本与 `IMG_1185` 重复。 |
| `IMG_1187.HEIC` | OnRobot USB pen drive 侧视。 | software/docs 资产。 | USB 内容未检查。 |
| `IMG_1188.HEIC` | OnRobot USB pen drive 顶视。 | software/docs 资产。 | 与 `IMG_1187` 重复，但保留。 |
| `IMG_1189.HEIC` | 箱体下层，包含 cable、Compute Box compartment、foam layout。 | inventory 和 repacking evidence。 | individual cable identity 需要结合 close-up。 |
| `IMG_1190.HEIC` | 黑色 sensor cable，看到 connector 和 stripped wire end。 | wiring bring-up 关键材料。 | 上电前必须查 pinout。 |
| `IMG_1191.HEIC` | 蓝色 Ethernet/UTP cable，疑似 packout 中 `UTP 0.5-m`。 | network bring-up 立即有用。 | 长度/category 未实测。 |
| `IMG_1192.HEIC` | Compute Box compartment / foam view。 | inventory、repacking evidence。 | ports 不可见。 |
| `IMG_1193.HEIC` | cable connector 和 desiccant。 | 低优先级 inventory evidence。 | 单独技术价值低。 |
| `IMG_1194.HEIC` | coiled black sensor/device cable。 | wiring、strain relief planning。 | 具体 cable role 需要 manual 对照。 |
| `IMG_1195.HEIC` | cable connector close-up。 | connector type 辅助判断。 | 较模糊，单独价值低。 |
| `IMG_1196.HEIC` | cable label，能看到 Woodhead Connectivity/Brad Harrison marking。 | 后续维护或替换线缆时有用。 | full part number 部分被遮挡。 |
| `IMG_1197.HEIC` | PG16 cable gland 侧视。 | 安装相关 spare。 | 具体安装位置待确认。 |
| `IMG_1198.HEIC` | PG16 cable gland 正面。 | 安装相关 spare。 | 与后续几张重复。 |
| `IMG_1199.HEIC` | PG16 cable gland 另一个正面角度。 | inventory evidence。 | 重复视角。 |
| `IMG_1200.HEIC` | PG16 cable gland 侧面另一个角度。 | 安装相关 spare。 | 重复视角。 |
| `IMG_1201.HEIC` | PG16 cable gland 斜视。 | inventory evidence。 | 重复视角。 |
| `IMG_1202.HEIC` | Compute Box foam tray / compartment。 | repacking evidence。 | 单独技术价值低。 |
| `IMG_1203.HEIC` | Compute Box port panel：24V、DEVICE、USB、DIP switch、Ethernet、LED、固定 IP `192.168.1.1`。 | bring-up 关键照片。 | 改任何 DIP 前先记录 live state。 |
| `IMG_1204.HEIC` | Compute Box 顶/侧面 label。 | identity evidence。 | 这面看不到 serial/model detail。 |
| `IMG_1205.HEIC` | Compute Box 外壳和 OnRobot logo。 | inventory evidence。 | 外观照，技术信息少。 |

## 硬件盘点结论

立即有用：

- HEX-E v2 sensor body 和 serial label。
- Compute Box，尤其是 port panel 和固定 IP。
- 24V power supply。
- Ethernet/UTP cable。
- sensor/device cable set。
- Adapter flange A 和 screw/accessory set，但要先查 manual 再装。

有保留价值但不是第一优先级：

- USB pen drive：可能有 installer 或 docs，但尚未检查内容。
- PG16 cable gland 和黑色小附件：具体用途取决于 manual 和最终 cable route。
- packing slip、calibration certificate、box label：属于 traceability evidence。

短期技术价值低但仍应保留：

- 重复 accessory 视角。
- foam/packaging layout 照片。
- Compute Box exterior-only 照片。

## 第一阶段 Bring-Up Checklist

先做 no-motion bring-up。

1. 传感器 unloaded、无接触。
2. 上电前拍 live cable routing 和 DIP switch state。
3. 用 24V adapter 给 Compute Box 供电。
4. 电脑直连 Compute Box Ethernet。
5. 电脑配置到能访问 `192.168.1.1` 的静态 IPv4 subnet。
6. ping `192.168.1.1`。
7. 找到官方 OnRobot Compute Box interface/manual，再发送任何命令。
8. 从 Compute Box 读取 raw wrench data，记录 6 个轴。
9. 稳定后把每次 run 存到 UR10e measurements 目录，不再依赖 `/tmp`。

独立 no-load stream 没确认前，不进入 robot-mounted active contact。

## TASE 接触实验前测试清单

Vendor-agnostic tests：

- Zero drift：unloaded、no contact；如果数据通路支持 zero，记录 zero 前后。
- Thermal drift：从 cold start 到至少 2 h。
- Noise floor：静态 unloaded 下每个轴的 RMS/std。
- Repeatability：重复 zero/unload cycles 和 known-load poses。
- Hysteresis：同一 known load 或 controlled contact 下做 load/unload。
- Cross-axis coupling：施加近似单轴 load，看其他轴 leakage。
- Frame alignment：用 gravity/tool orientation 验证 force direction。
- Payload/TCP/gravity consistency：对比 sensor reading 和 UR payload/TCP assumption。
- Cable strain/EMI sensitivity：轻微移动 cable，检查没有外力时读数是否偏移。

UR10e warm-up baseline：

- 需要测一次 30 min vs 2 h，作为 baseline。
- 最少记录 cold start、30 min、2 h 三个点。
- 每个点同时记录 OnRobot external wrench 和 UR internal `actual_TCP_force`。
- robot stationary、unloaded，除非该轮明确使用 known load。
- 用这次 baseline 决定后续 TASE run 是否需要固定 warm-up 时间；不能沿用旧 UR5e 结论。

TASE acceptance ladder：

1. No-contact long baseline。
2. Known-load 或 gravity sanity check。
3. 低风险 static contact，force limit 和 emergency stop 可达。
4. 慢速 quasi-static surface contact。
5. 最后才进入 finite-time surface-contact reproduction run。

## Robotiq / OnRobot / 坤维判断

PBT group 售前反馈：Robotiq 很可能最好用，因为它和 UR workflows 直接集成。

工程判断：

- Robotiq 可能赢在日常集成和 setup time。
- OnRobot 的价值在于它现在可用，可以先把实验 workflow 验出来。
- 坤维仍可能在 PC-side data access 和成本上有优势，但 UR 集成路径可能没那么直接。
- 对 TASE 来说，关键不是“哪个最省事”单一指标，而是 data access 是否稳定、update rate 是否够、latency 是否可控、frame 是否清楚、logging 是否可复现。

因此：保留 vendor-agnostic protocol，把 OnRobot-specific facts 单独记录；等 Robotiq 到位后用同一套测试表复测。

## 待确认问题

- Compute Box raw data 的官方 manual/API 应该以哪份文件为准？
- Adapter flange A 哪一面是 robot-side，哪一面是 tool-side？
- 黑色 accessory bag 中哪些是 UR mounting 必需件，哪些只是 cable holder 或 spare？
- USB pen drive 是否有可用 URCap installer？
- `Compute Box -> 电脑` 通路的实际 sampling frequency、latency 和 jitter 是多少？

## 文件位置

- Raw photos: `/Users/andyl/Documents/UR10e/ft_sensor/onrobot/hex_e_v2_3010007655/photos_raw/`
- Registry: `/Users/andyl/Documents/UR10e/.codex_context/photos/photo_registry.yml`
- Photo index: `/Users/andyl/Documents/UR10e/.codex_context/photos/PHOTO_INDEX.md`
- Move manifest: `/Users/andyl/Documents/UR10e/ft_sensor/onrobot/hex_e_v2_3010007655/move_manifest_20260518.csv`
- Registry rollback backup: `/Users/andyl/Documents/UR10e/archive/backups/onrobot_hex_e_v2_photo_registry_20260519_000810/`

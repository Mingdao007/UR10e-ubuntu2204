# UR10e OnRobot HEX-E v2 复现准备报告

## 核心结论

这是一套借来的 OnRobot `HEX-E v2` force/torque sensor，用来在 UR10e 上复现 TASE finite-time control 前先跑通硬件盘点、无运动 bring-up、force logging 和传感器 baseline。

已从照片确认：型号 `HEX-E v2`，S/N `3010007655`，套件为 `HEX-E with Compute Box and Adapter flange A`，box item `100453`，Compute Box 固定 IP 标注为 `192.168.1.1`。第一阶段先走 `Compute Box -> 电脑`，不先依赖 URCap/PolyScope。

OnRobot 现在的定位是临时验证平台。PBT group 售前反馈说 Robotiq 很可能是长期更省事的选择，因为它和 UR workflows 直接集成；但对 TASE 来说，最后仍要用同一套测试表核验数据频率、latency、frame 定义和 logging 稳定性。

## Visual Overview

先看这三张 contact sheet。它们是本报告的视觉索引：第一张是文档、UR10e 现场和传感器主体；第二张是传感器细节、附件和主要线缆；第三张是线缆、PG16 cable gland 和 Compute Box。

本轮已对 `IMG_1167.HEIC`、`IMG_1168.HEIC`、`IMG_1187.HEIC`、`IMG_1205.HEIC` 做原始 HEIC 正放修正；下方 JPEG previews 和 contact sheets 已重新生成，rotation 已写入像素，不再依赖 viewer 解释 EXIF Orientation。

![Contact sheet 1: documents, bench, sensor body](assets/contact_sheets/contact_sheet_01.jpg)

![Contact sheet 2: sensor details and accessories](assets/contact_sheets/contact_sheet_02.jpg)

![Contact sheet 3: cables, cable gland, compute box](assets/contact_sheets/contact_sheet_03.jpg)

## 读者任务地图

- 要确认“这到底是什么传感器”：看“身份与文档证据”。
- 要准备第一次上电：看“Bring-up 必需件”和“Compute Box”。
- 要判断能不能装到 UR10e：看“安装接口与 sensor frame 待确认项”。
- 要做 TASE 前测试：看“测试计划”和“Robotiq / OnRobot / 坤维判断”。

## 身份与文档证据

| 视觉证据 | 说明 |
|---|---|
| ![Box packout diagram](assets/thumbnails/IMG_1166.jpg) | **箱盖 packout diagram。** 这张是 inventory checklist：Compute Box、pen drive、HEX sensor、adapter/screw set、screw tool、PG16 cable gland、24V adapter/cable、UTP cable、sensor cable 都在图上。<br>原图：`IMG_1166.HEIC`；用途：开箱核对；不确定项：无明显不确定。 |
| ![Calibration certificate](assets/thumbnails/IMG_1167.jpg) | **Calibration certificate。** 证书对应 S/N `3010007655`，manufacturing ID `HEXEB306`，factory calibration date `2021-09-06`。<br>原图：`IMG_1167.HEIC`；用途：traceability；不确定项：证书不等于本地验收，仍要测 zero drift、thermal drift 和 noise。 |
| ![Historical repair packing slip](assets/thumbnails/IMG_1168.jpg) | **Repair/warranty packing slip。** 日期 `2022-11-15`，内容更像历史 RG2 V2 repair/warranty 文件。<br>原图：`IMG_1168.HEIC`；用途：文档归档；不确定项：不作为当前 HEX-E bring-up 依据。 |
| ![Main HEX-E packing slip](assets/thumbnails/IMG_1169.jpg) | **Main packing slip。** 写有 `HEX-E with Compute Box and Adapter flange A`，列出多个 serial，其中包括 `3010007655`。<br>原图：`IMG_1169.HEIC`；用途：借用/套件身份追踪；不确定项：当前实物身份以 sensor label 为准。 |
| ![Box specification label](assets/thumbnails/IMG_1170.jpg) | **箱体规格标签。** 明确是 HEX-E、Adapter flange A、Compute Box、item `100453`、S/N `3010007655`。<br>原图：`IMG_1170.HEIC`；用途：型号和配置确认；不确定项：无明显不确定。 |

## UR10e 现场与当前打印末端

| 视觉证据 | 说明 |
|---|---|
| ![UR10e bench overview](assets/thumbnails/IMG_1171.jpg) | **UR10e bench 总览。** 能看到当前 UR10e、打印末端和实验台环境。<br>原图：`IMG_1171.HEIC`；用途：TASE 复现实验现场 context；不确定项：不能证明 force sensor wiring。 |
| ![Printed end-effector near table](assets/thumbnails/IMG_1172.jpg) | **当前打印末端近景。** 展示末端靠近光学平台/接触区域的几何关系。<br>原图：`IMG_1172.HEIC`；用途：接触几何、clearance、末端形状判断；不确定项：不能直接确认最终 TCP 或 contact frame。 |

## Bring-Up 必需件

这些是第一次无运动上电和数据读取最直接相关的物料。

| 视觉证据 | 说明 |
|---|---|
| ![24V power adapter](assets/thumbnails/IMG_1173.jpg) | **24V power adapter。** XP Power adapter，输出 `24V 1.0A`，用于 Compute Box。<br>原图：`IMG_1173.HEIC`；用途：上电必需件；不确定项：本地插头/插座兼容性需要确认。 |
| ![Ethernet cable](assets/thumbnails/IMG_1191.jpg) | **蓝色 Ethernet/UTP cable。** 疑似 packout 中 `UTP 0.5-m`。<br>原图：`IMG_1191.HEIC`；用途：电脑与 Compute Box 网络连接；不确定项：长度/category 未实测。 |
| ![Sensor cable with stripped end](assets/thumbnails/IMG_1190.jpg) | **黑色 sensor/device cable。** 能看到 connector 和 stripped wire end。<br>原图：`IMG_1190.HEIC`；用途：sensor/Compute Box wiring；不确定项：上电前必须查官方 pinout。 |
| ![Coiled black cable](assets/thumbnails/IMG_1194.jpg) | **coiled black cable。** 可能是 sensor/device cable 的主要线束。<br>原图：`IMG_1194.HEIC`；用途：wiring 和 strain relief planning；不确定项：具体 cable role 要和 manual 对照。 |
| ![Cable label](assets/thumbnails/IMG_1196.jpg) | **Woodhead / Brad Harrison cable label。** 后续替换线缆或查 connector 时有价值。<br>原图：`IMG_1196.HEIC`；用途：maintenance/spare evidence；不确定项：full part number 部分被遮挡。 |

## 传感器主体与安装接口

| 视觉证据 | 说明 |
|---|---|
| ![Open kit layout](assets/thumbnails/IMG_1175.jpg) | **开箱总览。** sensor、Compute Box、USB drive、bagged accessories、cable 和 foam layout 都在这一张里。<br>原图：`IMG_1175.HEIC`；用途：missing-part check、repacking；不确定项：小附件细节不完全可见。 |
| ![Adapter flange A front](assets/thumbnails/IMG_1176.jpg) | **Adapter flange A 一面。** 可见 circular register 和 bolt holes。<br>原图：`IMG_1176.HEIC`；用途：机械安装兼容性检查；不确定项：哪面朝 robot/tool 要查 manual。 |
| ![Adapter flange A back](assets/thumbnails/IMG_1178.jpg) | **Adapter flange A 另一面。** 可见 recess 和 bolt pattern。<br>原图：`IMG_1178.HEIC`；用途：adapter 方向判断；不确定项：screw assignment 需要 manual 确认。 |
| ![HEX-E protected face](assets/thumbnails/IMG_1179.jpg) | **HEX-E 黑色保护面。** 展示 sensor 的一侧外观和中心特征。<br>原图：`IMG_1179.HEIC`；用途：orientation evidence；不确定项：sensor frame 方向需要 manual 确认。 |
| ![HEX-E side connector](assets/thumbnails/IMG_1180.jpg) | **HEX-E 侧面 connector。** 适合看 cable route 和 connector position。<br>原图：`IMG_1180.HEIC`；用途：cable routing、strain relief；不确定项：与相邻视角有重复。 |
| ![HEX-E logo side](assets/thumbnails/IMG_1181.jpg) | **HEX-E logo side。** 展示 OnRobot logo 和 connector 相对位置。<br>原图：`IMG_1181.HEIC`；用途：orientation evidence；不确定项：与 `IMG_1180` 接近。 |
| ![HEX-E serial label](assets/thumbnails/IMG_1182.jpg) | **关键 serial label。** 明确写有 `HEX-E v2`、`100453/IP67`、S/N `3010007655`。<br>原图：`IMG_1182.HEIC`；用途：实物身份确认；不确定项：无明显不确定。 |
| ![HEX-E mounting face](assets/thumbnails/IMG_1183.jpg) | **mounting face。** 可见 circular interface、O-ring、threaded holes 和 OnRobot quick interface feature。<br>原图：`IMG_1183.HEIC`；用途：安装方向和 interface 判断；不确定项：robot-side/tool-side 方向要查 manual。 |

## 附件、USB 与包装

这些不是第一秒就要接线的东西，但长期资产盘点需要保留，因为借来的套件后面还要能还原、复查、转方案比较。

| 视觉证据 | 说明 |
|---|---|
| ![Power adapter safety leaflet](assets/thumbnails/IMG_1174.jpg) | **power adapter safety leaflet。** 技术价值不高，但属于套件文档。<br>原图：`IMG_1174.HEIC`；用途：归档；不确定项：无。 |
| ![Silver accessory bag](assets/thumbnails/IMG_1184.jpg) | **银色 accessory hardware。** 可能与 screws、tool 或 cable holder 有关。<br>原图：`IMG_1184.HEIC`；用途：安装相关资产；不确定项：具体内容要拆包/manual 对照。 |
| ![Black accessory bag view 1](assets/thumbnails/IMG_1185.jpg) | **黑色 accessory pieces。** 可能是 mounting 或 cable-holder 相关件。<br>原图：`IMG_1185.HEIC`；用途：安装相关资产；不确定项：每个黑色件具体功能待确认。 |
| ![Black accessory bag view 2](assets/thumbnails/IMG_1186.jpg) | **黑色 accessory pieces 另一角度。** 保留用于缺件追踪。<br>原图：`IMG_1186.HEIC`；用途：inventory evidence；不确定项：与 `IMG_1185` 重复。 |
| ![OnRobot USB drive side](assets/thumbnails/IMG_1187.jpg) | **OnRobot USB pen drive 侧视。** 可能有 URCap installer 或 docs。<br>原图：`IMG_1187.HEIC`；用途：software/docs asset；不确定项：USB 内容未检查。 |
| ![OnRobot USB drive top](assets/thumbnails/IMG_1188.jpg) | **OnRobot USB pen drive 顶视。** 与上一张一起记录 U 盘外观。<br>原图：`IMG_1188.HEIC`；用途：software/docs asset；不确定项：重复视角。 |
| ![Lower box layer](assets/thumbnails/IMG_1189.jpg) | **箱体下层。** 记录 cable、Compute Box compartment 和 foam layout。<br>原图：`IMG_1189.HEIC`；用途：inventory、repacking；不确定项：individual cable identity 需要结合 close-up。 |
| ![Compute box compartment](assets/thumbnails/IMG_1192.jpg) | **Compute Box compartment / foam view。** 主要用于回装和 inventory。<br>原图：`IMG_1192.HEIC`；用途：repacking evidence；不确定项：ports 不可见。 |
| ![Connector and desiccant](assets/thumbnails/IMG_1193.jpg) | **connector + desiccant context。** 单独技术价值低，但说明箱内状态。<br>原图：`IMG_1193.HEIC`；用途：inventory evidence；不确定项：低优先级。 |
| ![Cable connector closeup](assets/thumbnails/IMG_1195.jpg) | **cable connector close-up。** 可辅助判断 connector type。<br>原图：`IMG_1195.HEIC`；用途：connector identification；不确定项：图像较模糊。 |

## PG16 Cable Gland

PG16 cable gland 暂时不是 first bring-up 的核心，但如果后续要走正式 cable route、strain relief 或 enclosure routing，它会变得有用。

| 视觉证据 | 说明 |
|---|---|
| ![PG16 cable gland side](assets/thumbnails/IMG_1197.jpg) | **PG16 cable gland 侧视。** 原图：`IMG_1197.HEIC`；用途：安装相关 spare；不确定项：具体安装位置待确认。 |
| ![PG16 cable gland front](assets/thumbnails/IMG_1198.jpg) | **PG16 cable gland 正面。** 原图：`IMG_1198.HEIC`；用途：安装相关 spare；不确定项：重复视角。 |
| ![PG16 cable gland front alternate](assets/thumbnails/IMG_1199.jpg) | **PG16 cable gland 另一正面角度。** 原图：`IMG_1199.HEIC`；用途：inventory evidence；不确定项：重复视角。 |
| ![PG16 cable gland side alternate](assets/thumbnails/IMG_1200.jpg) | **PG16 cable gland 侧面另一个角度。** 原图：`IMG_1200.HEIC`；用途：安装相关 spare；不确定项：重复视角。 |
| ![PG16 cable gland angled](assets/thumbnails/IMG_1201.jpg) | **PG16 cable gland 斜视。** 原图：`IMG_1201.HEIC`；用途：inventory evidence；不确定项：重复视角。 |

## Compute Box

| 视觉证据 | 说明 |
|---|---|
| ![Compute Box tray](assets/thumbnails/IMG_1202.jpg) | **Compute Box foam tray / compartment。** 主要记录包装位置。<br>原图：`IMG_1202.HEIC`；用途：repacking evidence；不确定项：单独技术价值低。 |
| ![Compute Box port panel](assets/thumbnails/IMG_1203.jpg) | **Compute Box port panel。** 关键照片：可见 24V、DEVICE、USB、DIP switch、Ethernet、LED 和固定 IP `192.168.1.1`。<br>原图：`IMG_1203.HEIC`；用途：bring-up 核心证据；不确定项：改任何 DIP 前先记录 live state。 |
| ![Compute Box side label](assets/thumbnails/IMG_1204.jpg) | **Compute Box 顶/侧面 label。** 记录外观和 label 面。<br>原图：`IMG_1204.HEIC`；用途：identity evidence；不确定项：这面看不到 serial/model detail。 |
| ![Compute Box outer case](assets/thumbnails/IMG_1205.jpg) | **Compute Box 外壳。** OnRobot logo 外观照。<br>原图：`IMG_1205.HEIC`；用途：inventory evidence；不确定项：技术信息少。 |

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

- Raw photos: `photos_raw/`
- Visual assets: `assets/thumbnails/`, `assets/contact_sheets/`
- Visual assets manifest: `assets/visual_assets_manifest.json`
- Orientation manifest: `orientation_manifest_20260519.json`, `orientation_manifest_20260519.csv`
- Move manifest: `move_manifest_20260518.csv`
- 旧版报告备份：`onrobot_hex_e_v2_reproduction_prep_20260518.before_md_report_skill.md`
- Registry rollback backup: `/Users/andyl/.codex/backups/recoverability-gate-md-report-skill-20260519_003624/`
- Orientation rollback backup: `/Users/andyl/.codex/backups/onrobot-orientation-md-report-skill-20260519_011959/`

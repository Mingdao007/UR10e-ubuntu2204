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
| ![Box packout diagram](assets/thumbnails/IMG_1166.jpg) | **箱盖 packout diagram。** 看到了什么：箱盖图把套件应包含的部件画成编号清单，包括 Compute Box/EtherCAT Converter、pen drive、HEX-E/HEX-H sensor、Adapter & Screw Set、screw tool、PG16 cable gland、24V universal mount adapter、24V robot power cable、UTP cable 和 sensor cable。<br>为什么有用：这是之后检查“缺了什么”的第一层证据，特别适合和实物照片逐项对照，避免把线缆、PG16、USB 这类小件当成无关包装。<br>下一步动作：把 packout 中每个编号和后续照片中的实物建立一一对应；如果后续安装发现 screw 或 cable holder 缺件，回到这张图确认它是否本来属于套件。<br>原图：`IMG_1166.HEIC`；风险/不确定项：它只能证明套件设计应包含这些东西，不能证明当前借来的箱子里每件都完整或适配 UR10e。 |
| ![Calibration certificate](assets/thumbnails/IMG_1167.jpg) | **Calibration certificate。** 看到了什么：证书写明 HEX serial number `3010007655`，HEX manufacturing ID `HEXEB306`，factory calibration date `2021-09-06`，签名和 OnRobot certificate 页脚可见。<br>为什么有用：这是传感器身份和 factory calibration traceability 的核心证据，后续报告里引用零漂、温漂、known-load 检查时可以把本地结果和这张证书关联起来。<br>下一步动作：实机 bring-up 后建立本地 baseline，不要把 certificate 当作当前性能合格证明；记录测试日期、温度、zero 操作和采样频率。<br>原图：`IMG_1167.HEIC`；风险/不确定项：证书只能说明出厂或历史校准状态，不能替代本次 UR10e 上的 zero drift、thermal drift、noise floor、cross-axis coupling 验收。 |
| ![Historical repair packing slip](assets/thumbnails/IMG_1168.jpg) | **Repair/warranty packing slip。** 看到了什么：这张 packing slip 日期为 `2022-11-15`，描述像 `Repair RG2 V2 Warranty`，serial number 与当前 HEX-E serial 不同。<br>为什么有用：它说明箱内混有历史维修或借用流转文件，不能所有文档都默认属于当前 HEX-E 套件；这能防止后续把错误文件当成传感器依据。<br>下一步动作：归档但降权处理，在最终实验记录里只作为“箱内历史文档”证据，不拿它判断 OnRobot HEX-E 的 bring-up、校准或接口。<br>原图：`IMG_1168.HEIC`；风险/不确定项：这张不应作为当前 HEX-E 资产身份或性能依据，除非之后能从借用方确认它和本套件流转有关。 |
| ![Main HEX-E packing slip](assets/thumbnails/IMG_1169.jpg) | **Main HEX-E packing slip。** 看到了什么：packing slip 描述为 `HEX-E with Compute Box and Adapter flange A`，列出多个 demo serial，其中包括 `3010007655`；日期和 shipping 信息也可见。<br>为什么有用：它把当前传感器、Compute Box、Adapter flange A 三者放进同一个套件语境，是借用资产追踪和配置确认的重要证据。<br>下一步动作：用这张和 sensor label、box specification label 交叉确认 S/N；还机或换传感器时，用它核对是否混入其他 demo serial。<br>原图：`IMG_1169.HEIC`；风险/不确定项：packing slip 列出多个 serial，当前实物身份必须以 sensor body label 和 box label 为准。 |
| ![Box specification label](assets/thumbnails/IMG_1170.jpg) | **箱体规格标签。** 看到了什么：箱体标签明确标出 HEX-E 规格，adapter 类型为 Adapter flange A，包含 Compute Box，item `100453`，S/N `3010007655`。<br>为什么有用：这是最直接的外包装身份确认，能快速回答“这套是不是 HEX-E v2 + Compute Box + Adapter flange A”。<br>下一步动作：在实验记录首页保留 item、serial、adapter 信息；查 manual 时优先查与 HEX-E、Adapter flange A、Compute Box 组合匹配的版本。<br>原图：`IMG_1170.HEIC`；风险/不确定项：箱标不能证明箱内每个部件未被替换，所以仍要和实物 label 逐项核对。 |

## UR10e 现场与当前打印末端

| 视觉证据 | 说明 |
|---|---|
| ![UR10e bench overview](assets/thumbnails/IMG_1171.jpg) | **UR10e bench 总览。** 看到了什么：UR10e 位于光学平台旁，当前打印末端已经装在 wrist 端，示教器在平台前方，周围有可触达的工作台和接触区域。<br>为什么有用：这张给 TASE 复现实验提供现场 context，能判断测试空间、示教器位置、应急操作可达性和相机/电脑摆放可能性。<br>下一步动作：安装 force sensor 后重新拍一张同角度照片，补充线缆走向、Compute Box 放置位置和 emergency stop 可达性；正式 contact control 前确认机械臂路径不扫到箱体或线缆。<br>原图：`IMG_1171.HEIC`；风险/不确定项：它不能证明 force sensor 已接入，也不能说明当前 TCP、payload、safety limit 或 fixture 是否已经设置。 |
| ![Printed end-effector near table](assets/thumbnails/IMG_1172.jpg) | **当前打印末端近景。** 看到了什么：打印末端靠近光学平台表面，末端形状、接触头和平台孔阵关系可见，当前没有 force sensor 夹在 flange 与末端之间。<br>为什么有用：它是复现 TASE 前的几何 baseline，可以帮助判断插入 HEX-E 后 TCP 会增加多少高度、接触点是否仍能落在平台可控区域。<br>下一步动作：安装 HEX-E 和 adapter 后测量新 TCP offset，重新确认 contact normal、clearance、tool mass 和 cable strain relief；不要沿用这张图对应的 TCP。<br>原图：`IMG_1172.HEIC`；风险/不确定项：照片无法给出精确尺寸，也不能确认接触材料、表面法向、末端刚度或最终 contact frame。 |

## Bring-Up 必需件

这些是第一次无运动上电和数据读取最直接相关的物料。

| 视觉证据 | 说明 |
|---|---|
| ![24V power adapter](assets/thumbnails/IMG_1173.jpg) | **24V power adapter。** 看到了什么：XP Power adapter 和圆形电源 connector 可见，adapter 标签显示输出为 `24V 1.0A`，外形和箱盖 packout 中的 24V power supply 对得上。<br>为什么有用：Compute Box bring-up 的第一步就是稳定供电，这张图说明当前套件里确实有独立 24V 电源，不必一开始从 UR 控制柜取电。<br>下一步动作：上电前确认插头、极性、connector 插入方向和线缆外皮状态；首次上电只接 Compute Box，不接 robot motion，不做 active contact。<br>原图：`IMG_1173.HEIC`；风险/不确定项：照片不能证明 adapter 输出实际稳定，也不能证明本地插座/转换头可靠，最好用万用表或 Compute Box LED 状态做 sanity check。 |
| ![Ethernet cable](assets/thumbnails/IMG_1191.jpg) | **蓝色 Ethernet/UTP cable。** 看到了什么：一根短蓝色 shielded-looking Ethernet cable，两端 RJ45 connector 可见，长度看起来适合电脑到 Compute Box 直连。<br>为什么有用：第一阶段数据通路计划是 `Compute Box -> 电脑`，这根线就是最直接的 network bring-up 物料。<br>下一步动作：电脑配置同网段静态 IPv4 后先 ping `192.168.1.1`，再打开官方 interface；如果连接不稳定，再换已知可靠网线排除 cable 问题。<br>原图：`IMG_1191.HEIC`；风险/不确定项：照片不能确认线缆 category、屏蔽状态、内部断线或实际长度；不要把通信失败直接归因于 Compute Box。 |
| ![Sensor cable with stripped end](assets/thumbnails/IMG_1190.jpg) | **黑色 sensor/device cable。** 看到了什么：一端是圆形 metal connector，另一端可见 stripped wires 和端子，说明这根线很可能涉及 device wiring 或 power/signal breakout。<br>为什么有用：这是最需要谨慎的 bring-up 物料。它可能决定 sensor、Compute Box 或外部 device 的连接方式，直接影响是否能读到 raw wrench stream。<br>下一步动作：上电前必须查官方 pinout 和接线示意，标记每根线对应功能；未确认 pinout 前不要把裸线接到电源、UR IO 或 Compute Box 端口。<br>原图：`IMG_1190.HEIC`；风险/不确定项：照片不能确认每根线的功能、是否需要端接、是否适合当前 Compute Box 配置；错误接线可能损坏设备。 |
| ![Coiled black cable](assets/thumbnails/IMG_1194.jpg) | **coiled black cable。** 看到了什么：黑色较长线缆卷绕在一起，两端圆形 connector 可见，线缆外皮文字可读性有限但能看出它是套件中的主线束之一。<br>为什么有用：安装到 UR10e 后，线缆长度、connector 方向和 strain relief 会影响力传感器读数稳定性，尤其是 no-contact baseline 和 cable strain sensitivity test。<br>下一步动作：先确认它与 `IMG_1190` 是否同一线缆或同类线缆；正式安装时拍照记录线缆从 wrist 到 Compute Box 的路径，并做轻微 cable movement test。<br>原图：`IMG_1194.HEIC`；风险/不确定项：仅凭照片不能判断它到底是 sensor cable、robot power cable 还是备用线，必须和 manual 或线缆标签核对。 |
| ![Cable label](assets/thumbnails/IMG_1196.jpg) | **Woodhead / Brad Harrison cable label。** 看到了什么：线缆黄色标签和外皮标识可见，能读到 `Woodhead Connectivity Brad Harrison` 之类信息，外皮上也有线规/温度/认证文字。<br>为什么有用：后续如果需要替换线缆、查 connector family、追 cable spec，这张比普通线缆全景更有维护价值。<br>下一步动作：实物上再拍一张正对 label 的高清近照，记录完整 part number；把线缆角色和对应端口写入 bring-up log。<br>原图：`IMG_1196.HEIC`；风险/不确定项：当前照片没有完整露出所有 part number，不能单独作为采购或接线依据。 |

## 传感器主体与安装接口

| 视觉证据 | 说明 |
|---|---|
| ![Open kit layout](assets/thumbnails/IMG_1175.jpg) | **开箱总览。** 看到了什么：sensor body、Compute Box、USB drive、bagged accessories、线缆和 foam cutouts 同框出现，能看到套件在箱内的层级关系。<br>为什么有用：这是还原包装、确认缺件、判断哪些小件属于同一套的总览图。后续如果临时拆装，很容易用这张恢复箱内摆放。<br>下一步动作：每次借出或还回前按这张拍对照图；拆黑色附件包或线缆包后补一张 unpacked inventory。<br>原图：`IMG_1175.HEIC`；风险/不确定项：总览不够清楚，不能识别每个 screw、cable holder 或 connector 的具体型号。 |
| ![Adapter flange A front](assets/thumbnails/IMG_1176.jpg) | **Adapter flange A 一面。** 看到了什么：圆形 register、多个 bolt holes 和一个较小定位/特征孔可见，表面有手写或刻印的 `A`。<br>为什么有用：这是判断 Adapter flange A 能否把 OnRobot interface 转接到 UR10e 或当前末端的关键机械证据。<br>下一步动作：用 manual 确认这面是 robot-side 还是 tool-side；安装前量 bolt circle、孔径和凸台高度，确认不会和打印末端或 HEX-E body 干涉。<br>原图：`IMG_1176.HEIC`；风险/不确定项：照片不能给出精确尺寸，也不能确认螺丝规格和扭矩。 |
| ![Adapter flange A back](assets/thumbnails/IMG_1178.jpg) | **Adapter flange A 另一面。** 看到了什么：另一侧有 recess、沉孔/槽位和不同的 bolt pattern，边缘缺口能帮助判断安装方向。<br>为什么有用：和 `IMG_1176` 组合后，可以在不拿错面的情况下准备装配路线，避免把 adapter 翻反导致 cable route 或 register 不匹配。<br>下一步动作：把两面都和 OnRobot mounting manual 对照，确定 robot-side、sensor-side、tool-side 的顺序，再安排螺丝和垫片。<br>原图：`IMG_1178.HEIC`；风险/不确定项：不能凭照片确定 screw assignment，尤其不能假设所有孔都要上螺丝。 |
| ![HEX-E protected face](assets/thumbnails/IMG_1179.jpg) | **HEX-E 黑色保护面。** 看到了什么：sensor 一侧是黑色圆形保护/接口面，中心孔和周围结构可见，侧边 connector 从圆周边缘伸出。<br>为什么有用：这张提供 sensor body 的空间占用和 cable exit 方向，对 UR10e wrist 附近避让、线缆弯折半径和视觉 frame 标记都有用。<br>下一步动作：安装前决定 connector 朝向，尽量让 cable 不被 wrist motion 拉扯；安装后用同角度照片记录最终方向。<br>原图：`IMG_1179.HEIC`；风险/不确定项：黑色面是否对应 robot-side 或 tool-side 不能只靠照片判断，必须查 manual。 |
| ![HEX-E side connector](assets/thumbnails/IMG_1180.jpg) | **HEX-E 侧面 connector。** 看到了什么：圆周侧面 connector、OnRobot logo、黑色隔离层和金属壳体的相对位置可见。<br>为什么有用：这张主要服务 cable routing。安装到 UR10e 后，connector 朝向会决定线缆是否扫到 robot link、打印末端或光学平台。<br>下一步动作：规划 cable strain relief 时以这张为参考，确认 connector 不承担线缆重量；进入 baseline 测试前做 no-contact cable movement check。<br>原图：`IMG_1180.HEIC`；风险/不确定项：它与 `IMG_1181` 是相近视角，不能额外确认 sensor frame 定义。 |
| ![HEX-E logo side](assets/thumbnails/IMG_1181.jpg) | **HEX-E logo side。** 看到了什么：OnRobot logo、侧面 marker、connector 和 sensor 外壳关系更清楚，能看到 logo 与黑色隔离层的位置。<br>为什么有用：这是 `IMG_1180` 的补充视角，适合在安装后对照“传感器有没有转到预期角度”，尤其是记录 frame alignment 前的物理方向。<br>下一步动作：安装完成后把 logo 朝向写进实验记录，并用 gravity pose 检查 force direction 是否与软件 frame 一致。<br>原图：`IMG_1181.HEIC`；风险/不确定项：logo 朝向不等价于官方 force/torque frame，不能用 logo 直接定义坐标系。 |
| ![HEX-E serial label](assets/thumbnails/IMG_1182.jpg) | **关键 serial label。** 看到了什么：label 明确写有 `MODEL HEX-E v2`、item `100453/IP67`、S/N `3010007655`，旁边还有 cable holder 标记。<br>为什么有用：这是当前实物身份的最强证据，优先级高于 packing slip。所有 baseline、日志文件名、借用记录都应该绑定这个 serial。<br>下一步动作：把 S/N 写进数据采集脚本输出目录或 run metadata；如果以后换 Robotiq 或坤维，不要把 OnRobot baseline 混进新 sensor 数据。<br>原图：`IMG_1182.HEIC`；风险/不确定项：label 不提供当前 calibration health，也不说明 Compute Box firmware 或 API version。 |
| ![HEX-E mounting face](assets/thumbnails/IMG_1183.jpg) | **mounting face。** 看到了什么：圆形接口、中心结构、O-ring/密封样特征、threaded holes 和 OnRobot quick interface feature 可见。<br>为什么有用：这是判断机械安装方向、接触面清洁度、O-ring 状态和 adapter 贴合风险的关键图。<br>下一步动作：安装前检查这面是否有灰尘、异物或划痕；按 manual 确认与 Adapter flange A 的配合方向和螺丝规格。<br>原图：`IMG_1183.HEIC`；风险/不确定项：照片无法确认螺纹是否完好、O-ring 是否压缩正常，也不能替代 torque-controlled assembly。 |

## 附件、USB 与包装

这些不是第一秒就要接线的东西，但长期资产盘点需要保留，因为借来的套件后面还要能还原、复查、转方案比较。

| 视觉证据 | 说明 |
|---|---|
| ![Power adapter safety leaflet](assets/thumbnails/IMG_1174.jpg) | **power adapter safety leaflet。** 看到了什么：电源适配器随附安全说明纸张，和 `IMG_1173` 的 power adapter 属于同一电源相关物料。<br>为什么有用：技术信息不如 adapter label 直接，但它证明安全/合规文档在箱内，后续归还或审计时不应丢弃。<br>下一步动作：不需要作为 bring-up 的主要依据；如果出现电源异常，再查 adapter model 对应 datasheet。<br>原图：`IMG_1174.HEIC`；风险/不确定项：这张不能确认输出电压、电流、极性或 connector pinout。 |
| ![Silver accessory bag](assets/thumbnails/IMG_1184.jpg) | **银色 accessory hardware。** 看到了什么：透明袋内有银色小五金和白色/金属件，旁边可见 foam cutout，像是 adapter/screw set 或 cable holder 相关附件。<br>为什么有用：这些小件可能决定能否完整安装 Adapter flange A 或固定线缆，丢失会直接影响机械装配和 strain relief。<br>下一步动作：拆包前拍更清楚的 itemized photo，按 manual 把每个 screw/tool/cable holder 对到安装步骤。<br>原图：`IMG_1184.HEIC`；风险/不确定项：袋内具体件数和功能当前无法完全确认，不能凭照片直接选螺丝。 |
| ![Black accessory bag view 1](assets/thumbnails/IMG_1185.jpg) | **黑色 accessory pieces。** 看到了什么：透明袋内有多个黑色塑料/橡胶件，形状不同，旁边能看到 sensor body 一角。<br>为什么有用：这组黑色件很可能和 mounting、cable holder 或保护件有关，属于“现在不一定用，但装配失败时会很关键”的物料。<br>下一步动作：拆包后逐件拍照并编号；查 manual 确认哪些是 UR10e 安装必需件，哪些只是 spare 或 cable management。<br>原图：`IMG_1185.HEIC`；风险/不确定项：当前不能给每个黑色件命名，也不能判断是否全部需要装。 |
| ![Black accessory bag view 2](assets/thumbnails/IMG_1186.jpg) | **黑色 accessory pieces 补充视角。** 看到了什么：同一类黑色附件从另一角度拍摄，袋内件的轮廓和数量相对 `IMG_1185` 更容易交叉核对。<br>为什么有用：不重复解释功能，保留它主要是为了缺件追踪。如果后续拆包发现件数对不上，可用两张图一起回溯。<br>下一步动作：和 `IMG_1185` 合并为一个 accessory group 处理，拆包后更新清单。<br>原图：`IMG_1186.HEIC`；风险/不确定项：仍无法识别具体用途，不能替代 manual。 |
| ![OnRobot USB drive side](assets/thumbnails/IMG_1187.jpg) | **OnRobot USB pen drive 侧视。** 看到了什么：OnRobot branded USB drive 在 foam 内，USB connector 露出，说明箱内有独立 software/docs 介质。<br>为什么有用：USB 可能包含 URCap installer、manual、example programs 或旧版工具，对 PolyScope 集成和官方文档核对有潜在价值。<br>下一步动作：在隔离电脑或只读方式下读取目录结构，记录文件名、版本号和日期；不要直接把未知 installer 装到 UR 控制器。<br>原图：`IMG_1187.HEIC`；风险/不确定项：照片不能证明 USB 内容存在、版本可用或安全。 |
| ![OnRobot USB drive top](assets/thumbnails/IMG_1188.jpg) | **OnRobot USB pen drive 顶视。** 看到了什么：同一 USB drive 的品牌和外观更完整，位置和 foam cutout 关系清楚。<br>为什么有用：这是 `IMG_1187` 的补充记录，主要用于还原包装和证明 USB 确实属于这套 OnRobot kit。<br>下一步动作：内容读取后，把 USB 内文件清单链接回这两张照片；如果内容为空或不可读，也记录下来。<br>原图：`IMG_1188.HEIC`；风险/不确定项：重复视角，不增加软件内容证据。 |
| ![Lower box layer](assets/thumbnails/IMG_1189.jpg) | **箱体下层。** 看到了什么：箱体下层 foam 中可见 cable coil、Compute Box compartment、USB/connector 区域和蓝色 Ethernet cable。<br>为什么有用：这张把多个 accessories 的相对位置放在一起，适合还原 packaging 和判断哪些线缆原本在同一层。<br>下一步动作：每次拆箱后按这张恢复线缆和 Compute Box 的摆放，避免 connector 被压弯；必要时标注每个 foam cutout 对应物料。<br>原图：`IMG_1189.HEIC`；风险/不确定项：单看这张不能识别 individual cable role，必须结合 close-up。 |
| ![Compute box compartment](assets/thumbnails/IMG_1192.jpg) | **Compute Box compartment / foam view。** 看到了什么：Compute Box 所在的 foam compartment 和旁边干燥剂/小件位置可见，但 port panel 不在视野内。<br>为什么有用：它是 packaging evidence，能帮助还原 Compute Box 如何放回箱内，避免端口和 cable gland 被挤压。<br>下一步动作：归还前按这张检查 Compute Box 放置方向；实际 bring-up 仍以 `IMG_1203` 的 port panel 为准。<br>原图：`IMG_1192.HEIC`；风险/不确定项：看不到端口和 DIP switch，不能用于配置网络或接线。 |
| ![Connector and desiccant](assets/thumbnails/IMG_1193.jpg) | **connector + desiccant context。** 看到了什么：一个 cable connector 靠近干燥剂，说明线缆端头原本放在箱内 foam 周边。<br>为什么有用：单独技术价值低，但它补充了箱内状态和 connector 存放位置，能帮助判断是否有潮湿/运输相关风险。<br>下一步动作：实际使用前检查 connector pin 是否干净、无弯折、无异物；干燥剂不用进入实验记录主线。<br>原图：`IMG_1193.HEIC`；风险/不确定项：照片较局部，不能确认 connector type 或线缆功能。 |
| ![Cable connector closeup](assets/thumbnails/IMG_1195.jpg) | **cable connector close-up。** 看到了什么：线缆端头和一段外皮文字可见，背景是电脑屏幕和实验台。<br>为什么有用：它辅助识别 connector type 和线缆规格，和 `IMG_1194/1196` 一起构成线缆维护证据。<br>下一步动作：如果需要查替代线或延长线，重新拍清晰正对 connector 的照片，并量 pin 数、keying 和外径。<br>原图：`IMG_1195.HEIC`；风险/不确定项：当前图像偏模糊，不适合作为采购或 pinout 的唯一依据。 |

## PG16 Cable Gland

PG16 cable gland 暂时不是 first bring-up 的核心，但如果后续要走正式 cable route、strain relief 或 enclosure routing，它会变得有用。

| 视觉证据 | 说明 |
|---|---|
| ![PG16 cable gland side](assets/thumbnails/IMG_1197.jpg) | **PG16 cable gland 侧视。** 看到了什么：黑色 PG16 cable gland 的螺纹、锁紧帽和主体轮廓可见。<br>为什么有用：如果后续要把线缆穿过 enclosure、控制柜板或正式 cable route，这个件可用于 strain relief 和 cable pass-through。<br>下一步动作：先查 OnRobot manual 里它对应哪一步，确认是否与 Compute Box、robot cable 或 enclosure 安装有关；临时桌面 bring-up 阶段可以先不使用。<br>原图：`IMG_1197.HEIC`；风险/不确定项：不能确认适配哪根 cable，也不能确认是否必须用于 UR10e open-bench setup。 |
| ![PG16 cable gland front](assets/thumbnails/IMG_1198.jpg) | **PG16 cable gland 正面。** 看到了什么：从孔口方向看到内部开口和锁紧结构。<br>为什么有用：这是 `IMG_1197` 的补充视角，用于判断 cable 是否可能穿过和是否有 rubber insert。<br>下一步动作：和实物量内径，确认是否匹配 sensor cable 外径；不需要在第一轮 no-motion bring-up 中强行安装。<br>原图：`IMG_1198.HEIC`；风险/不确定项：重复视角，不能独立给出安装位置。 |
| ![PG16 cable gland front alternate](assets/thumbnails/IMG_1199.jpg) | **PG16 cable gland 正面补充角度。** 看到了什么：同一 PG16 件从略不同角度拍摄，孔口、外轮廓和桌面参照更清楚。<br>为什么有用：用于 inventory evidence 和后续缺件比对，不重复解释功能。<br>下一步动作：拆装后如果这个件缺失或被替换，用 `IMG_1197-1201` 整组核对。<br>原图：`IMG_1199.HEIC`；风险/不确定项：仍不能确认是否属于当前安装必需件。 |
| ![PG16 cable gland side alternate](assets/thumbnails/IMG_1200.jpg) | **PG16 cable gland 侧面补充角度。** 看到了什么：侧面螺纹和锁紧帽比例比 `IMG_1197` 更直观。<br>为什么有用：如果需要向别人描述或找同类件，这张比正面图更能表达外形。<br>下一步动作：只作为 spare/inventory 图保留；实际采购仍要看型号或量尺寸。<br>原图：`IMG_1200.HEIC`；风险/不确定项：没有可读型号，不能作为采购依据。 |
| ![PG16 cable gland angled](assets/thumbnails/IMG_1201.jpg) | **PG16 cable gland 斜视。** 看到了什么：斜视图显示整体高度、端部开口和外壳形状。<br>为什么有用：给 PG16 group 一个完整外观补充，便于还原物料和判断是否损坏。<br>下一步动作：如果正式 cable route 需要它，再单独建 cable gland 安装记录；否则只在 inventory 中保留。<br>原图：`IMG_1201.HEIC`；风险/不确定项：重复视角，不能新增功能判断。 |

## Compute Box

| 视觉证据 | 说明 |
|---|---|
| ![Compute Box tray](assets/thumbnails/IMG_1202.jpg) | **Compute Box foam tray / compartment。** 看到了什么：Compute Box 外壳放在 foam tray 中，周边开槽和保护结构清楚。<br>为什么有用：这是包装和运输保护证据，能帮助归还时避免端口面受压。它也说明 Compute Box 是独立盒体，不是直接集成在 sensor 内。<br>下一步动作：正式接线时把 Compute Box 从 foam 中取出，避免散热、线缆弯折和端口受力；归还前按这张恢复。<br>原图：`IMG_1202.HEIC`；风险/不确定项：看不到端口，不能指导 wiring。 |
| ![Compute Box port panel](assets/thumbnails/IMG_1203.jpg) | **Compute Box port panel。** 看到了什么：端口面可见 `24V` power connector、`DEVICE` connector、micro USB、DIP switch、`ETHERNET` RJ45、DEVICE/COMPUTE BOX LEDs，以及固定 IP 标注 `192.168.1.1`。<br>为什么有用：这是第一阶段 `Compute Box -> 电脑` bring-up 的核心图。它直接告诉我们供电口、传感器/device 口、电脑 Ethernet 口和固定 IP 入口。<br>下一步动作：上电前拍 live DIP switch state；接 24V 后观察 LED；电脑设到同网段后 ping `192.168.1.1`；确认官方 API/手册后再读 raw wrench stream。<br>原图：`IMG_1203.HEIC`；风险/不确定项：不要随意改 DIP switch。照片只能显示标签和端口，不能证明 firmware version、API endpoint、sampling rate 或 sensor 已被正确识别。 |
| ![Compute Box side label](assets/thumbnails/IMG_1204.jpg) | **Compute Box side label。** 看到了什么：Compute Box 外壳侧面写有 `COMPUTE BOX`，周围 foam 和 cable gland 位置可见。<br>为什么有用：它是 `IMG_1203` 的外观补充，帮助确认这个银色盒体确实是 Compute Box，而不是普通 accessory enclosure。<br>下一步动作：如需记录资产外观或还原包装，保留此图；实际接线仍以 port panel 图为准。<br>原图：`IMG_1204.HEIC`；风险/不确定项：这面没有 serial、firmware、端口或 IP 信息。 |
| ![Compute Box outer case](assets/thumbnails/IMG_1205.jpg) | **Compute Box outer case。** 看到了什么：Compute Box 外壳和 OnRobot logo 清楚，能看到整体尺寸比例和金属外壳形态。<br>为什么有用：用于资产外观记录和归还检查，也能提醒后续放置时避免让 cable 直接拉拽盒体。<br>下一步动作：bring-up 时给 Compute Box 固定一个不妨碍 robot motion 的位置，线缆留应力释放；后续如果换 Robotiq 或坤维，这张只作为 OnRobot 资产记录。<br>原图：`IMG_1205.HEIC`；风险/不确定项：技术信息少，不能用于网络配置或 pinout 判断。 |

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

## 第一次上手：不安装到 UR10e 的 bench drift 测试

这一节是给“第一次摸 OnRobot HEX-E”的现场步骤。目标不是让 UR10e 动，也不是安装 URCap，而是先确认这套 `HEX-E v2 -> Compute Box -> 电脑` 能独立上电、联网、显示 6-axis wrench，并且能在 no-contact 状态下记录一段 no-load baseline drift 和 warm-up drift。

今天这一步不要打断 UR10e 当前正在跑的零漂。OnRobot 先独立放桌面或稳定底板上测，后面真正装到 UR10e wrist 后还要重新测一次 mounted baseline。

### 0. 先认清今天要用的部件

| 照片 | 现场动作 |
|---|---|
| ![HEX-E serial label](assets/thumbnails/IMG_1182.jpg) | 先拿起 sensor body，确认 label 写的是 `MODEL HEX-E v2`、S/N `3010007655`。之后数据文件、截图、日志目录都用这个 serial 绑定，避免以后和 Robotiq、坤维或其他 OnRobot demo unit 混在一起。 |
| ![Compute Box port panel](assets/thumbnails/IMG_1203.jpg) | 找到 Compute Box 端口面。今天只关心 `24V`、`DEVICE`、`ETHERNET`、LED、DIP switch 和固定 IP 标签 `192.168.1.1`。DIP switch 先拍照记录，不要改。 |
| ![24V power adapter](assets/thumbnails/IMG_1173.jpg) | 找到 24V adapter。它最后再插入 Compute Box，先不要上电。上电前看 connector、线皮、adapter label 是否明显损坏。 |
| ![Ethernet cable](assets/thumbnails/IMG_1191.jpg) | 找到蓝色 Ethernet/UTP cable。它用于电脑和 Compute Box 直连，不经过 UR10e。 |
| ![Sensor cable with stripped end](assets/thumbnails/IMG_1190.jpg) | 找到 sensor/device cable。只使用已确认匹配的 round connector 端；裸线端今天不接任何电源、UR IO、控制柜或未知端口。 |
| ![Coiled black cable](assets/thumbnails/IMG_1194.jpg) | 如果 `IMG_1190` 和现场实物不完全对应，再用这张核对另一根黑色线缆。不要凭外观猜 pinout。 |
| ![Cable label](assets/thumbnails/IMG_1196.jpg) | 如果通信或供电异常，拍清楚线缆 label 和 connector，再查 manual 或问借用方。不要用这张照片反推出接线定义。 |

### 1. 摆放 sensor 和 Compute Box

| 照片 | 现场动作 |
|---|---|
| ![Open kit layout](assets/thumbnails/IMG_1175.jpg) | 参考开箱总览，把 sensor、Compute Box、线缆都放在同一张稳定桌面上。sensor 不要悬空，不要压线，不要让 connector 承重。 |
| ![Lower box layer](assets/thumbnails/IMG_1189.jpg) | 从箱内取线缆时保持 connector 干净，不要硬拽 foam 里的线缆。取出后让线缆自然弯曲，留一点 slack。 |
| ![Compute Box tray](assets/thumbnails/IMG_1202.jpg) | Compute Box 正式上电时从 foam tray 里拿出来，放在桌面上散热。不要让线缆拉着盒体，也不要把盒体放到 robot motion path 附近。 |

摆放完成后先拍一张 live photo，记录 sensor 姿态、线缆走向、Compute Box 位置和 DIP switch 当前状态。这个 live photo 后面可以解释 drift 里是否有 cable strain。

### 2. 按顺序接线

按这个顺序做，目的是先完成低风险物理连接，最后才上电。

1. 确认 UR10e 当前实验不需要你碰 wrist、tool、控制柜或示教器。
2. 把 HEX-E 放平或固定到稳定底板上，保持 no contact、unloaded。
3. 用确认过的 sensor cable round connector 接 HEX-E sensor body。
4. 另一端接 Compute Box 的 `DEVICE` 口。不要用蛮力拧 connector；对准 keying 后再锁紧。
5. 用蓝色 Ethernet cable 连接 Compute Box 的 `ETHERNET` 口和电脑的 Ethernet adapter。
6. 最后把 24V adapter 接到 Compute Box 的 `24V` 口，再接电源。
7. 上电后等大约 1 min，观察 Compute Box LED 是否从启动状态进入稳定状态；如果 LED 一直异常，先断电检查线缆，不要继续乱试 DIP switch。

异常时的第一原则：先拍照记录，再断电复查 physical connection。不要在不确定 pinout 的情况下把裸线端接到任何东西上。

### 3. 电脑网络设置

Compute Box 标签写着固定 IP `192.168.1.1`，所以电脑要放到同一个 network subnet（网络子网）。Mac 上优先用图形界面做：

1. 打开 `System Settings -> Network`。
2. 选择当前有线 Ethernet adapter，可能叫 `USB 10/100/1000 LAN`、`USB Ethernet` 或类似名字。
3. 进入 `Details -> TCP/IP`。
4. `Configure IPv4` 选 `Manually`。
5. `IP Address` 填 `192.168.1.2`。
6. `Subnet Mask` 填 `255.255.255.0`。
7. `Router` 可以留空；如果系统强制填写，可先填 `192.168.1.1`，但不要把这个网络接入普通实验室 LAN。
8. Apply 后等几秒。

Terminal sanity check：

```bash
ping 192.168.1.1
```

正常情况是能看到稳定 reply。若 ping 不通，按顺序检查：Compute Box 是否上电、Ethernet 线是否插紧、Mac 是否选对 adapter、电脑 IP 是否真的变成 `192.168.1.2`、是否有另一个设备占用了 `192.168.1.1`。

### 4. 打开 Web Client，只做最小确认

浏览器打开：

```text
http://192.168.1.1
```

如果能进入 Web Client，先只做这几件事：

1. 截图保存登录页或首页，文件名建议包含 `hex_e_v2_3010007655` 和时间。
2. 如果页面要求登录，先试 manual 里提到的默认信息 `admin` / `OnRobot`；这条来自较新的 OnRobot UR manual，当前借来的 HEX-E v2 仍需现场验证。
3. 如果页面要求你设置新 password，先暂停并截图。因为这会改变借来的 Compute Box 状态，除非已经和借用方确认，否则不要随手改。
4. 进入 Devices 或 HEX 相关页面后，确认能看到 `Fx`、`Fy`、`Fz`、`Tx`、`Ty`、`Tz`。如果只看到 device error，先记录错误码和截图。

今天 Web Client 的作用是确认设备活着、能显示 force/torque 数值，并做低频人工记录。高频 logging、sampling rate、latency 和 jitter 后面需要确认官方 raw stream/API 后再写脚本，不在第一次上手里硬猜。

### 5. 先记录 power-on raw values，再决定是否 zero

第一次看到 6 个轴时，不要马上按 zero。先记录一组 power-on raw values：

- time from power on：例如 `00:03:00`
- sensor 姿态：例如 `flat on table, label side visible`
- 是否有 tool 或 adapter 挂在 sensor 上：今天应为 no
- `Fx Fy Fz Tx Ty Tz`
- Compute Box LED state
- screenshot filename

然后在确认 sensor 无接触、线缆没有被拉、手没有碰 sensor 的情况下，可以做一次 temporary zero。这里的 zero 是 Web Client 或 URCap 里的临时读数归零，不等于 Auto-calibration，也不等于 factory calibration。zero 后马上再记录一组数，标成 `after temporary zero`。

如果你想保留最干净的 warm-up drift，建议今天分两段：

1. `raw drift before zero`：上电后前 10 min 不按 zero，观察原始 offset 如何走。
2. `drift after temporary zero`：10 min 时做一次 temporary zero，然后继续记到 2 h。

这样未来能分清“开机本身的 offset”和“zero 后的剩余漂移”。

### 6. 2 h warm-up drift 记录表

最小记录模板如下。截图不需要每秒截，关键时间点截即可。

| time_from_power_on | room_temp_C | Compute Box LED | Fx_N | Fy_N | Fz_N | Tx_Nm | Ty_Nm | Tz_Nm | screenshot_filename | touched_cable | notes |
|---|---:|---|---:|---:|---:|---:|---:|---:|---|---|---|
| 00:00:30 |  |  |  |  |  |  |  |  |  | no | just powered on |
| 00:03:00 |  |  |  |  |  |  |  |  |  | no | first visible Web Client values |
| 00:10:00 |  |  |  |  |  |  |  |  |  | no | before temporary zero |
| 00:10:30 |  |  |  |  |  |  |  |  |  | no | after temporary zero |
| 00:15:00 |  |  |  |  |  |  |  |  |  | no |  |
| 00:30:00 |  |  |  |  |  |  |  |  |  | no |  |
| 01:00:00 |  |  |  |  |  |  |  |  |  | no |  |
| 02:00:00 |  |  |  |  |  |  |  |  |  | no |  |

今天先不要追求漂亮数字。更重要的是：是否能稳定连上、读数是否连续、zero 后是否明显飘、线缆轻微位置变化是否会让数值跳。

### 7. 今天先别做

- 不把 HEX-E 装到 UR10e wrist 上，避免污染正在跑的 UR10e 零漂 baseline。
- 不改 DIP switch。先拍照，后查 manual，再决定。
- 不做 firmware update。借来的设备不要第一次上手就升级。
- 不做 Auto-calibration。OnRobot manual 对 HEX auto-calibration 有 mounted orientation 相关要求，bench 放桌面时做会让结果没有上机意义，还会改变设备状态。
- 不把裸线端接到 24V、UR IO、控制柜、breadboard 或任何未知端口。
- 不让 sensor 承受冲击、悬挂载荷或 cable pull。
- 不把 Web Client temporary zero 当成传感器校准证明。

### 8. 成功判据和下一步

今天算成功，只需要满足：

1. sensor identity 确认为 `HEX-E v2`，S/N `3010007655`。
2. Compute Box 能上电，LED 进入稳定状态。
3. 电脑能 ping 到 `192.168.1.1`。
4. Web Client 能打开，能看到或至少识别 HEX device。
5. 能记录一组 `Fx Fy Fz Tx Ty Tz`。
6. no-contact 状态下完成至少 30 min 记录；理想情况完成 2 h。

下一步不是马上装 UR10e，而是把这次表格和截图放进同一归档目录，再决定是否写 PC-side logging 脚本。等 high-rate data path 清楚后，再进入 mounted baseline、frame alignment、payload/TCP consistency 和 TASE contact ladder。

### Source notes

- ✅ confirmed: [OnRobot HEX product page](https://onrobot.com/en/products/hex-6-axis-force-torque-sensor) 说明 HEX 是 6-axis force/torque sensor，输出语义对应 `Fx`、`Fy`、`Fz`、`Tx`、`Ty`、`Tz`。
- ✅ confirmed: [OnRobot HEX-E v2 manual mirror on ManualsLib](https://www.manualslib.com/manual/3875848/Onrobot-Hex-E-V2.html) 包含旧版 HEX-E v2 UR manual 信息，包括 Compute Box 默认 IP `192.168.1.1`、URCap zero 和 USB/URCap workflow。
- ✅ confirmed: [OnRobot UR manual PDF mirror](https://media.ecosphere-solutions.de/instructions/de/onrobot/HEX/User_Manual_For_UR_Robots_HEX-E_H_QC_v1.17.0_EN.pdf) 记录了 Web Client 访问、factory default IP `192.168.1.1`、默认登录信息 `admin` / `OnRobot`、Web Client force/torque display、Zero toggle，以及 Auto-calibration 与安装姿态相关的注意事项。该 PDF 面向较新的 HEX-E/H QC workflow，本报告只把它作为现场参考行为，当前借来的 HEX-E v2 仍以实物、Web Client 和后续官方文件核对为准。

# 讨论（第二轮）：摩擦/法向可辨识性、NO-v2 机制证据与一个共同外环修复实验

角色：advisory，非阻塞，无 live authority，main 裁定。所有输入均为 development 数据，
不是 holdout；未改控制器/测试/默认参数，未运行任何闭环 campaign，未提交。
本目录只含只读后处理与开环观测器回放：`scripts/` 可复算，`data/` 为派生数字。
逐 tick 缓存写在 `/tmp/ynv2/`（不保留）；派生 JSON 全部保留并列入 `data/manifest.json`。
开环回放只检验观测模型，不是闭环结果：轨迹由另一估计器生成，估计→力轴→法向运动的耦合缺席。

English abstract. Legacy estimator tracks the resultant force direction (error equals
the friction angle; true load equals 5 cos delta within 0.04 N). NO-forceoff-v1 never
moved from the approach prior (drift at most 0.003 deg), so its 1.3 deg is the prior.
NO-v2 fails through a rectification instability of the normalized motion-tangent update:
zero-mean normal velocity is squared into drift away from truth, with no restoring term
for the cross-sliding error component. A coplanarity residual n.(f x v) observes exactly
that component, needs no friction coefficient, and is immune to normal velocity. One
bounded closed-loop experiment (NO-v3, 18 full cycles, six pre-registered rejection
criteria) is proposed in section 6. The premise should narrow to a declared prior cone,
and friction must be physically identified before any compensation.

## 0. 结论摘要

1. **旧估计器是合力方向跟踪器，不是曲面法向估计器。** 名义工况前 20 s，n̂ 与 −f̂ 的夹角
   仅 1.27° / 2.61°（刚性/柔软），与真法向的误差 6.24° / 17.01°，恰是传感器中的摩擦角
   （真法向与 −f̂ 夹角中位 7.1° / 18.4°）。控制器调节的是 |f| 沿 n̂ 的分量，于是真实法向
   载荷 = 5 cos δ：静态段实测 4.962 vs 4.968 N、4.755 vs 4.770 N，最大差 0.04 N。柔软材料
   0.28 N 的力偏差和 13–18° 的"姿态误差"都是摩擦角，既不是曲面，也不是任何控制律的属性。
2. **NO-forceoff-v1 是冻结先验。** 四个单元整周期内 n̂ 相对 approach 的变化 RMS ≤ 0.002°、
   最大 0.003°。旧运动更新未归一化，速率 ≈ motion_gain·|v|² ≈ 8 × (4.5 mm/s)² ≈ 1.6e-4 /s
   （时间常数约 100 min），从未起作用。README 中"关闭偏置后法向 RMS 降到 1.36°/1.29°"
   就是先验本身的 1.3–1.4° RMS（最大 ≤ 2.1°）。它证明的是"不估计"，不是"估计更好"。
3. **NO-v2 的失效是归一化运动更新的整流不稳定，不只是压入/释放污染。** 对零均值法向速度
   a(t)（力环振铃、接触弹跳、推力/释放），更新把 a² 整流成远离真法向的漂移：横向误差分量
   `dδ⊥/dt = +γ δ⊥ ⟨a²⟩/⟨|v|²⟩`（没有恢复项），沿滑动分量
   `dδ∥/dt = −γ δ∥ (s² − ⟨a²⟩)/(s² + ⟨a²⟩)`。合成验证：A/s = 0.3/0.7/1.0 时横向增长率
   0.039/0.152/0.242 /s（预测 0.043/0.197/0.333）。nov2-stiff-normal 中释放后 |v_n|/|v|
   中位从 0.36 升到 0.87，误差 10° → 39°；用同一速度记录做开环 NV 回放同样得到末值 39.9°。
   降低 γ 不能修复（判据是能量比，不是 γ）；对残差设门限会引入偏置（§3）。
4. **friction-balance 的"机制"成立，但只是外环恒等式。** 估计坐标系内路径偏移 = |P f|/Kp，
   forceoff 四单元逐 tick 残差 0.46–0.84 mm RMS。任何带路径弹簧的积分型导纳在稳态都如此，
   与法则无关。旧估计器路径误差小，是坐标系把摩擦吸收进"法向"：其自身坐标系切向力 RMS
   0.08 / 0.18 N，而真切向力 0.60 / 1.56 N。
5. **可辨识性结构是两条互补约束，各有一种明确的污染源。** 运动切向残差 `r1 = n̂·v` 观测
   沿滑动方向的误差 δ∥，被法向速度整流污染；共面残差 `r2 = n̂·(f×v)/|f×v|` 观测横向分量
   δ⊥，对法向速度、载荷和 μ 的数值都免疫（真法向残差 p99 ≤ 0.007，振铃单元 ≤ 0.016），
   只被不在 (n, v) 平面内的切向力污染（切向推力单元 p99 0.44–0.47）。μ 与同线人力在瞬时
   不可分，但共面约束只用"摩擦在 (n, v) 平面内"这一结构，不用 μ 值。
6. **开环回放**（γ = 0.3，cap 0.05 rad/s，ρ = sin 15°）：从 10° 先验误差出发，两残差观测器
   TRu 在 forceoff/legacy 名义单元 6–11 s 内降到 2° 以下，两个倾斜方向都收敛；NV 单独对横向
   倾斜在 forceoff/NO-v2 名义单元内整周期不收敛（末值 5.7–9.1°），legacy 单元需 23–43 s；
   CP 单独对纵向倾斜在 forceoff/NO-v2 名义单元内不收敛（末值 5.1–8.4°），legacy 单元需 17–50 s。
7. **前提需要收窄。** 本场景真法向偏离 approach 仅 1.3–1.4° RMS，"未知曲面"没有被激励；
   估计器唯一可检验的价值是对先验误差的鲁棒性，只能用倾斜先验探针检验。应声明"法向在
   approach 的 15° 锥内"。μ_eff、接触刚度和法向环振铃需要物理辨识；摩擦前馈在 μ 辨识前
   是赌注，失配代价 (μ̂ − μ_eff)·N/Kp，且必须声明对反向同线推力 < μ̂N 的不让步区。
8. **只提议一个实验 NO-v3（§6）**：18 个闭环完整周期，六条预注册拒绝判据，任一结果都有信息。

## 1. 数据与方法

14 个既有完整周期 receipt（DSFC，2 ms，8 子步），SHA-256 前 12 位：

| 单元 | 来源 | SHA |
|---|---|---|
| legacy stiff nominal / normal / tangent | yield-offset-ablation, yield-transfer-v1 | eadaddc8080a / a99ed3a75efe / 41301e4be54a |
| legacy compliant nominal / normal / tangent | yield-transfer-v1 | 21fa73cf68d5 / a9f6d6cf83e2 / 975ed9ac2843 |
| NO-v2 stiff nominal / normal | yield-normal-v2 | d438d62d3db0 / e94215e33472 |
| NO-v2 compliant nominal / normal | yield-normal-v2 | d2d4d42741fe / d5831bc28e0e |
| forceoff stiff nominal / normal | yield-normal-forceoff-v1 | a6157e58437d / 659d36b79f7e |
| forceoff compliant nominal / normal | yield-normal-forceoff-v1 | 85c112ce9977 / 0a8cbf3c7603 |

切向推力单元不在 NO-v2 报告内，但摩擦与让步的冲突只在那里出现，故一并读取。

脚本：`extract_ticks.py`（受理 records 中 observation/result 与 rows 中的评估真值，对齐
PATH tick）、`geometry.py`（角度、速度比、共面残差、摩擦恒等式，窗口 pre/int/post/all）、
`binned.py`（1 s 分箱时序）、`observer_replay.py` 与 `two_residual_replay.py`（开环回放）、
`rectification_demo.py`（无评估真值的合成验证）。派生数据：`data/geometry.json`、
`data/binned-1s.json`、`data/observer-replay.json`、`data/two-residual-replay.json`、
`data/rectification-demo.json`。

## 2. 三个估计器实际在做什么

### 2.1 legacy：合力方向

| 单元（前 20 s） | n̂ vs 真法向 | n̂ vs −f̂ | 真法向 vs −f̂ | n̂ vs approach |
|---|---:|---:|---:|---:|
| legacy stiff | 6.24° | 1.27° | 6.51° | 7.26° |
| legacy compliant | 17.01° | 2.61° | 17.50° | 17.99° |

力方向修正的锥 atan(0.35) = 19.3° 放行了 7–18° 的摩擦角；步长上限 0.004 rad/tick × 500 Hz
远快于其他项。切向推力时合力再倾 atan((2.5 − μN)/5) ≈ 19°，仍在锥内，坐标系被拉到
20.0° / 14.9°（干预窗 RMS），速度方向偏离参考 22–69°。姿态目标沿 n̂，于是 orientation
指标在 legacy 下度量的是摩擦角。这不是"未知曲面 + 姿态随动"，而是"随合力"。

### 2.2 forceoff：冻结先验

四单元 n̂ 相对 approach 的整周期变化 RMS 0.001–0.002°、最大 0.002–0.003°。先验误差
RMS 1.26–1.42°、最大 1.74–2.06°（曲面曲率 0.8/0.4 1/m 加 0.4 mm 正弦纹在 80×20 mm 范围内
只产生这么多）。摩擦全部进入柔顺项：估计坐标系切向力 0.600 / 1.409 N，路径偏移
5.05 / 11.84 mm，恒等式 |P f|/Kp − e_t 残差 0.46 / 0.47 mm（normal 单元 0.54 / 0.84 mm）。

### 2.3 NO-v2：法向速度占比与失控时序

真法向速度占总速度的比值 |v_n|/|v|（中位 / p90）：

| 窗口 | NO-v2 stiff | NO-v2 compliant | forceoff stiff | forceoff compliant |
|---|---|---|---|---|
| nominal 前 20 s | 0.099 / 0.35 | 0.116 / 0.70 | 0.041 / 0.14 | 0.061 / 0.71 |
| 法向推力 20–35.5 s | 0.297 / 0.70 | 0.371 / 0.74 | 0.161 / 0.36 | 0.357 / 0.70 |

法向运动是力调节本身固有的（forceoff 也有），估计误差把它放大一倍。nov2-stiff-normal
的 1 s 分箱：推力期间误差 5–7°；释放后 36 s 10.2°、40 s 12.0°、50 s 16.8°、55 s 24.0°、
60 s 36.5°、62 s 39.2°；同期 |v_n|/|v| 中位 0.36–0.52 升到 0.84–0.87，55–62 s 的法向速度 RMS
2.3–2.6 mm/s 已超过切向 RMS 1.6–2.0 mm/s（能量比 > 1）；真实载荷升到 6.2–6.8 N（力轴倾斜
后 |f|·cos 42° = 5 N），速度方向偏离参考 61–84°。全窗指标 14.83° RMS、峰值 9.74 N、
失接触 0.888 s 由此而来。

## 3. 归一化运动更新的整流不稳定

设真法向 n（固定），滑动速度 s t̂，法向速度 a(t)，估计 n̂ = n cos δ + ê sin δ，ê 为切平面内
单位向量，ψ 为 ê 与 t̂ 的夹角。NO-v2 更新 `n̂ ← n̂ − dt γ (n̂·v) P v / |v|²`。
按 a 展开 (n̂·v)·P v：

- 滑动项 s² sin δ cos ψ (t̂ − …)：只对 ψ = 0 的分量（δ∥）起作用，dδ∥/dt = −γ δ∥ s²/⟨|v|²⟩；
- 整流项 a² cos δ (n − cos δ n̂) = −a² sin δ cos δ t̂_est 方向：对**任意** ê 都把 n̂ 推离 n，
  dδ/dt = +γ δ ⟨a²⟩/⟨|v|²⟩；
- 线性项 a·s·cos 2δ t̂_est：对零均值 a 平均为零，但只要更新被按 |n̂·v| 的门限选择性跳过，
  被保留样本中 a 的均值就不再为零，线性项变成与 δ 同号的漂移。

合成验证（`rectification_demo.py`，s = 4.5 mm/s，a = A sin(2π·1.5 t)，μ = 0.15，初始 3°，
γ = 1，无 cap，20 s）：

| 观测器 | 倾斜 | A/s = 0 | 0.3 | 0.7 | 1.0 | 1.5 |
|---|---|---:|---:|---:|---:|---:|
| NV 末 2 s 均值 n̂ 误差 | 沿滑动 | 0.00° | 0.00° | 0.00° | 0.00° | 0.12° |
| NV 拟合增长率 /s（预测） | 横向 | 0 (0) | 0.039 (0.043) | 0.152 (0.197) | 0.242 (0.333) | 0.357 (0.529) |
| CP 末误差 | 沿滑动 / 横向 | 3.00 / 0.00 | 3.00 / 0.00 | 3.00 / 0.00 | 3.00 / 0.00 | 3.00 / 0.00 |
| TR（|r1| ≤ tan15°·|Pv| 门限） | 沿滑动 | 0.00 | 7.43 | 4.39 | 3.54 | 3.18 |
| TRu（NV 不设残差门限 + CP） | 横向 | 0.00 | 0.00 | 0.00 | 0.00 | 0.49 |

三点推论。(a) 横向分量对任何法向速度能量都不稳定，速率与 γ 成正比、与能量比成正比：
降 γ 只是同比例放慢收敛和发散。(b) 对残差设门限（TR）在 A/s = 0.3 时把 3° 偏到 7.4°，
慢窗能量门限（TRs，未列）在门限附近抖动同样偏置 8°：门限必须不依赖 n̂·v 的符号结构，
而最简单的做法是不设门限。(c) 共面项 CP 对横向分量提供 −γ δ⊥ 的恢复且与 a 无关，
TRu 的横向分量净速率 −(γ₂ − γ₁⟨a²⟩/⟨|v|²⟩) 恒为负；沿滑动分量仍要求 ⟨a²⟩ < s²。
NV 在 γ = 1 下的逐周期抖动 ±1–2.5°；用速率上限 ω_max 可把抖动限在 ω_max/(4 f_ring)：
0.05 rad/s、1.5 Hz 时 0.48°。

## 4. 共面约束与互补可辨识性

模拟接触力 f = N n − μ N r(v_t) + f_ext，v = v_t + a n，r 为正则化的 v_t 方向。
f × v = (N s + μ N a)(n × t̂)：与 a、μ、N 的值无关，n ⊥ (f × v) 恒成立；只在
a < −s/μ（μ = 0.4 时向外速度超过 2.5 倍滑速）时反号，而更新是 r2 的二次型，符号无关。
协议的三种外力方向（outward、tangent、oblique）都在 (n, t̂_ref) 平面内，共面性按构造保持；
**协议从未施加面外切向力**，这是评估设计的盲区，须记录。

真法向共面残差 |n·(f×v)|/(|f||v|) 的 p99：legacy/forceoff 名义与法向推力单元 0.0012–0.0068，
NO-v2 四单元 0.0044–0.0164（含振铃与失控段），切向推力单元 0.442 / 0.467（干预窗 RMS
0.211 / 0.226）。切向单元残差大有两个同时成立的来源：坐标系被拉偏 20–27° 使实际运动方向
与推力方向偏离 22–69°，以及推力沿 t̂_ref 而摩擦沿 −v_t；坐标系不被拉偏时前者消失，
后者在同线推力下为小量。这只能在闭环里验证，写入 §6 R5。

互补性：r1 只观测 δ∥（NV 对 ψ = 90° 无更新），r2 只观测 δ⊥（CP 对 ψ = 0 无更新）。
Lissajous 参考的方向扫过一周需要 63 s，纵向速度 4 cos(0.1t) 主导；单靠 NV 的横向分量、
单靠 CP 的纵向分量都只能借方向变化缓慢修正。开环回放（γ = 0.3，cap 0.05 rad/s，
ρ = sin 15°，接触/激励门沿用）从 10° 先验误差到 < 2° 的时刻（s）：

| 单元 | 倾斜方向 | NV | CP | TRu | TRu 末值 |
|---|---|---:|---:|---:|---:|
| forceoff stiff nominal | 横向 | 未达（末 7.2°） | 28.3 | 7.4 | 0.68° |
| forceoff stiff nominal | 纵向 | 7.2 | 未达（末 5.5°） | 6.6 | 0.68° |
| forceoff compliant nominal | 横向 | 未达（末 9.1°） | 36.3 | 10.5 | 0.39° |
| forceoff compliant nominal | 纵向 | 未达（末 2.8°） | 未达（末 8.4°） | 11.0 | 0.38° |
| legacy stiff nominal | 横向 / 纵向 | 43.3 / 6.3 | 17.5 / 49.8 | 6.8 / 6.0 | 0.73° |
| legacy compliant nominal | 横向 / 纵向 | 23.3 / 7.5 | 15.6 / 17.5 | 8.0 / 6.9 | 0.68° |

无倾斜时 TRu 全窗 RMS 0.49–0.85°（forceoff/legacy 名义），低于先验 1.3–1.4°；法向推力单元
1.0–1.4°；切向单元干预窗 2.5–2.6°（CP 门限只挡住一部分）。在 nov2-stiff-normal 这条失控轨迹上
（记录的能量比 > 1）TRu 释放后 6.2°，CP 单独 1.9°：这是记录轨迹的病态，不是闭环预测；
闭环下是否出现这种能量比正是 §6 R2 要判定的。

## 5. 摩擦、让步与"机制"评估

- 切向力 f_t = −μ N_c t̂ + f_h。摩擦的可用结构只有两点：方向反平行于 v_t，幅值正比于
  接触载荷 N_c。法向推力下 N_c = N_meas − f_h,n 不可观测（控制器把总力调到 5 N，接触载荷
  实为 2.5 N），同线反向人力与摩擦在瞬时无法区分。这是任务前提的硬边界，不是实现缺陷。
- 因此坐标系诚实的控制器必有路径偏移 f_t/Kp（本场景 5 / 12 mm），稳态与法则无关；
  法则差异只在让步/恢复瞬态。用 legacy 的小路径误差比较法则，等于把摩擦角当作坐标系。
- 模拟内有效 μ_eff = 真切向力/载荷 ≈ 0.12 / 0.29（正则化后）。实机对应量是无人参与的
  名义滑动：μ̂ = |P_a f| / (−a·f)，用 approach 坐标系即可，不需要估计器。
- 摩擦前馈 −μ̂ N̂ r(v_t) 在 μ̂ 辨识前是赌注：失配代价 (μ̂ − μ_eff) N/Kp，以 legacy 已声明的
  0.35 算，刚性材料会反向偏 8 mm；且它对 < μ̂N 的反向同线推力不让步，必须作为声明的
  不让步区写入协议。协议现有的切向推力是正向（沿运动），根本不触发这个区。本轮不提议。
- friction-balance.json 的数字与本文恒等式一致，可保留为"外环恒等式核验"，
  不应称为机制发现。

## 6. 提议的唯一实验：NO-v3（两残差共同观测器）闭环消融

### 6.1 观测器

输入（控制器内已有，不新增测量）：v（测得 TCP 线速度，base）、f（与控制器相同的 20 ms
滤波腕力，base）、in_contact、dt。不用 μ，不用曲面真值，不用指令速度（指令补偿会把
稳态滑动中 u_n = s tan δ 这唯一的几何信息一并减掉）。

完整状态：{ n̂ ∈ S²（单位内法向，base）, a（approach，固定） }。无其他状态。

    P      = I − n̂ n̂ᵀ
    r1     = n̂·v
    c      = f × v ;  ĉ = c/|c|  (|c| < 1e-9 时跳过 CP 项)
    r2     = n̂·ĉ
    g      = γ₁ r1 P v / max(|v|², ε²)            [接触门 ∧ 激励门]
           + γ₂ r2 P ĉ · 1[|r2| ≤ ρ]              [接触门 ∧ 激励门 ∧ 锥门]
    g      ← g · min(1, ω_max/|g|)
    n̂_next = normalize(n̂ − dt g)

门：接触门 |f| ≥ 1 N 且 −n̂·f ≥ 1 N，激励门 |P v| ≥ 2 mm/s（沿用）；NV 项**不设**残差门限
（§3(b)）；CP 锥门 ρ = sin 15° 是声明的先验锥，也是面外力的最大允许比例。
参数：γ₁ = γ₂ = 0.3 /s，ε = 2 mm/s，ω_max = 0.05 rad/s，ρ = 0.259。
接触丢失时保持状态，与现状一致。旧版默认参数与算术不变（γ₂ = 0、ρ = None 即退化为
NO-v2 形式；motion_normalization_floor 为 None 时保持 legacy 原算术）。

线性化误差动力学（可证伪的预测，不是稳定性证书）：

    dδ∥/dt = −γ₁ δ∥ (s² − ⟨a²⟩)/(s² + ⟨a²⟩)
    dδ⊥/dt = −γ₂ δ⊥ + γ₁ δ⊥ ⟨a²⟩/(s² + ⟨a²⟩) + γ₂ f⊥/|f|

其中 f⊥ 为不在 (n, v) 平面内的切向力。抖动上限 ω_max/(4 f_ring) ≈ 0.5°。
闭环额外耦合：倾斜 δ 使切向前馈产生真法向分量 s sin δ，力环以 u_n 补偿，稳态 a → 0，
瞬态 a 由振铃决定；这正是开环回放看不到、必须闭环判定的部分。

### 6.2 单元

固定 DSFC 机械参数、共同外环、QP、plant（2 ms，8 子步），完整周期，记录全状态。

| 臂 | 观测器 | 先验 | 材料 × 场景 | 新单元 |
|---|---|---|---|---:|
| A | legacy | approach | 既有 6（含 tangent） | 0 |
| B | forceoff（冻结先验） | approach | 既有 4 | 0 |
| C | NO-v3 | approach | 2 × {nominal, sustained_normal, sustained_tangent} | 6 |
| D | NO-v3 | approach 绕纵向轴倾 +10°（误差横向） | 2 × {nominal, sustained_normal} | 4 |
| E | NO-v3 | approach 绕横向轴倾 +10°（误差纵向） | 2 × {nominal, sustained_normal} | 4 |
| F | forceoff | 同 E 的倾斜先验 | 2 × nominal | 2 |
| G | NO-v3，γ₂ = 0 / γ₁ = 0 | 同 D 的倾斜先验 | stiff × nominal × 2 | 2 |
| 合计 | | | | 18 |

D/E 只是探针：倾斜先验通过 runner 旋转 approach 后同时传给控制器与估计器，曲面放置只依赖
origin，不受影响；姿态锚随 n̂ 传输，因此初始姿态目标也偏 10°，这正是要观察的代价。
G 是项消融：预期 γ₂ = 0 臂横向误差不收敛、γ₁ = 0 臂在 D 中收敛（与开环回放同向）。
按既有节奏（4 worker，4 单元约 1 min），约 5 min 墙钟。

### 6.3 预注册预测与拒绝判据

| 判据 | 预测 | 拒绝条件 → 结论 |
|---|---|---|
| R1 无害 | C-nominal 法向 RMS ≤ B + 0.3°，力 MAE ≤ B + 0.03 N，路径 RMS ≤ B + 0.5 mm，峰值 ≤ B + 0.2 N，无失接触 | 任一超出 → 估计在本场景为负价值，前提收窄为"冻结先验" |
| R2 法向推力免疫 | C-sustained_normal 中 n̂ 相对 20 s 时的倾斜全程 ≤ 3°，释放后法向 RMS ≤ 2.5°，失接触不多于 B（0 / 0.028 s），峰值 ≤ 6.3 N；闭环能量比 ⟨(n̂·v)²⟩/⟨|Pv|²⟩（2 s 窗）全程 < 0.5 | 超出 → 闭环整流未被 CP 项压住，NO-v3 在此增益下被拒 |
| R3 可辨识性 | D、E 在 nominal 中 30 s 内 < 2°，末 10 s RMS < 2°；E（NV 主导）快于 D（CP 主导） | D 失败 E 通过 → CP 项在闭环被拒，横向分量声明不可观测；两者失败 → 观测器在闭环不估计，前提收窄，估计器降为监视量 |
| R4 相对冻结先验的价值 | F 路径 RMS ≈ 5 sin10°/120 ≈ 7 mm 加摩擦偏移，姿态 ≈ 10°；D/E 在 30 s 后两者减半 | 未减半 → "未知曲面"前提在本场景无可测收益 |
| R5 切向推力 | C-sustained_tangent 干预窗共面残差 < 0.05，法向 RMS ≤ 3°；让步/恢复与 A 并列报告不排名 | 残差 > ρ 持续 > 2 s → CP 门已保持（安全），但"同线推力共面"假设在闭环被证伪，记录 |
| R6 摩擦恒等式 | 所有 NO-v3 单元估计坐标系路径偏移 = |P f|/Kp，RMS 差 ≤ 1 mm | 不成立 → 路径指标不能归因摩擦，需查外环 |

全部数据仍为 development；不得按结果回调 γ、ρ、ω_max 后声称同一实验通过。
入口瞬态保留在全窗指标中，稳态段只作解释。

### 6.4 实现触点（供 main 判断是否可接受）

- `contact_yield_normal.py`：新增可选参数 `coplanarity_gain_s_inv`（默认 0）与
  `coplanarity_cone_sin`（默认 None），进入 `parameters()` 与 identity；更新函数已接收 f。
- `contact_yield_runner.py`：新增 `prior_tilt_deg` / `prior_tilt_axis ∈ {along, lateral}`，
  旋转 approach 后同时用于 controller 与 estimator，并写入 artifact。
- 后处理复用本目录脚本；R2 的能量比与 R5 的共面残差可从 records 直接算。
- 旧默认 estimator 参数与算术不动；155 项回归应保持通过；新参数版本需要短程全状态回放。

### 6.5 本实验不能回答的

面外（横向）人力：协议不含，CP 污染速率 γ₂ f⊥/|f| 在 ρ 内有界、超过则保持，但未被检验；
真实 μ、接触刚度、伺服与法向环振铃未辨识；plant 关节速度硬裁剪与 state_age = 0 的乐观性
（round 1）不变。

## 7. 前提是否需要收窄、是否先做物理辨识

需要，且两者都先于任何新的法则变体。

- "未知曲面"→"法向在 approach 的声明锥（15°）内，滑动中有界修正"。本场景先验误差
  1.3–1.4°，估计器的价值只能由倾斜先验探针证明；不做探针就没有估计的证据。
- "姿态随动"指标在 legacy 下是摩擦角；SFC/DSFC/MSFC 的既有比较里柔软材料的力偏差与姿态
  误差主要来自坐标系，而非法则。法则比较必须在与 μ 无关的共同坐标系下重做。
- 物理辨识清单（实机，无人参与，approach 坐标系即可）：μ_eff（名义滑动的 |P_a f|/(−a·f)）、
  接触刚度（入口斜坡）、法向环振铃频率与阻尼（决定 ⟨a²⟩/s² 与 NV 项的安全域）。
- 摩擦补偿与不让步区只能在 μ_eff 辨识后作为声明的协议项引入，不能作为共同模块修复。
- 不建议再扩展控制律候选；先决定观测器（§6），再在共同坐标系下重做法则比较。

## 附：复算

```bash
# 从实验根目录；缓存写 /tmp/ynv2，不保留
D=report/yield-normal-v2/discussion
.venv-contact-six/bin/python $D/scripts/extract_ticks.py runs/yield-normal-v2/DSFC-stiff_low_mu-sustained_release_normal.json.gz /tmp/ynv2/nov2-stiff-normal.npz
.venv-contact-six/bin/python $D/scripts/geometry.py '/tmp/ynv2/*.npz'      > $D/data/geometry.json
.venv-contact-six/bin/python $D/scripts/binned.py '/tmp/ynv2/*.npz'        > $D/data/binned-1s.json
.venv-contact-six/bin/python $D/scripts/observer_replay.py '/tmp/ynv2/*.npz'   # 每文件 JSON
.venv-contact-six/bin/python $D/scripts/two_residual_replay.py /tmp/ynv2/forceoff-stiff-nominal.npz
.venv-contact-six/bin/python $D/scripts/rectification_demo.py > $D/data/rectification-demo.json
```

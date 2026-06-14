# Step5d Strict TASE RNN Mapping Review

**Date**: 2026-06-14  
**Scope**: 审查 Step5d strict TASE RNN reproduction 的 UR10e 映射方案是否忠实于论文  
**论文**: Xu et al. 2026, TASE, "Finite-Time Convergence Neural Network-Based Force-Motion Control for Unknown Surface With Orientation Compliance"  
**Verdict**: **有条件接受** — 3 项 Must Fix 必须在写任何实现代码之前解决

---

## 一、论文核心公式索引

实现时必须引用的 equation number，全部来自 PDF 原文：

| 内容 | Eq. |
|---|---|
| 微分运动学 `ẋ = J(θ)θ̇` | (4) |
| 位置规范矩阵 `Φ_E = Diag(1,1,0)` | (5) |
| 力规范矩阵 `Φ̃_E = I − Φ_E` | (6) |
| `Φ_O = R_d^T Φ_E`, `Φ̃_O = R_d^T Φ̃_E` | (7) |
| 运动速度 `ẋ_t = Φ_O(ẋ_pd + k_p e_p)` | (8) |
| 期望法向量 `u = F/‖F‖`，叉积矩阵 S | (9) |
| 期望旋转矩阵 `R_d = I + sin(u)S + (1−cos(u))S²` | (10) |
| 四元数定向误差 `e_qua = Q_d^{-1} ⊗ Q` | (13) |
| 定向控制 `ẋ_o = k_o e_o`（因 `ẋ_od = 0`，Remark 1） | (14) |
| 力控阻抗（K_d=0）`ẋ_p = (e_f + k_f∫e_f dt − B_d ẋ_p) / M_d` | (16) |
| 速度级重写（含通信延迟 T） | (17) |
| 速度上界 `ω⁺ = min(θ̇⁺, α(θ⁺ − θ))` | (18) |
| 速度下界 `ω⁻ = max(α(θ⁻ − θ), θ̇⁻)` | (19) |
| 完整 QP: `min θ̇ᵀθ̇/2`, s.t. `ẋ_c = J(θ)θ̇`, `ω⁻ ≤ θ̇ ≤ ω⁺` | (20) |
| Lagrangian `L = θ̇ᵀθ̇/2 + λᵀ(J(θ)θ̇ − ẋ_c)` | (21) |
| KKT + 投影 | (22a/b) |
| **RNN θ̇ 方程（印刷原文）** `ε θ̇_c = −sigr(θ̇ − P_Ω(θ̇ − (θ̇ − Jᵀ(θ)λ)))` | **(23a)** |
| **RNN θ̇ 方程（括号化简）** `ε θ̇_c = −sigr(θ̇ − P_Ω(Jᵀ(θ)λ))` | **(23a-simplified)** |
| **λ 符号说明** Eq.(23) 中的 λ 状态等价于 Eq.(21) 标准 Lagrange multiplier 的负值 | — |
| **RNN λ 方程** `ελ̇ = J(θ)θ̇ − ẋ_c` | **(23b)** |
| `sigr(x) = |x|^r sign(x)`, r∈[0,1]（Remark 5） | Fig.5 |

论文实验参数（Section VI，Franka 实验）：

```
ε = 0.022
k_p = 4, k_o = 5, k_f = 1
M_d = Diag(12, …, 12)
B_d = Diag(550, …, 550)
θ_i^± = ±3.0 rad
θ̇_i^± = ±0.15 rad/s   ← 与 step5d table qdot_cap=0.15 一致
f_d = −5 N
```

---

## 二、Must Fix（3 项，实现前必须全部解决）

### MF-1：RNN Solver 必须是有状态类

**问题**：当前 `StrictTaseRnnSolver.solve()` 是无状态调用，每次独立，没有跨 tick 持久化的 RNN 状态。  
**论文要求**：Eq.(23) 是连续时间 ODE，`θ̇_c` 和 `λ` 都是状态变量，必须跨 tick 积分。

**必须改法**：

```python
class StrictTaseRnnSolver:
    def __init__(self, config):
        # 验证 paper truth ...
        self.theta_dot_state = np.zeros(6)   # θ̇_c，跨 tick 持久
        self.lambda_state    = np.zeros(6)   # λ，跨 tick 持久

    def step(self, *, actual_q, J, xdot_c, omega_minus, omega_plus,
             dt, epsilon=0.022, r=0.2):
        # 1. 投影/饱和 P_Ω
        proj_input = J.T @ self.lambda_state              # Eq.(23a): +J^T lambda
        projected = np.clip(proj_input, omega_minus, omega_plus)
        # 2. RNN 驱动量 sigr
        sigr_arg = self.theta_dot_state - projected
        sigr_val = np.abs(sigr_arg)**r * np.sign(sigr_arg)   # Eq.23a
        # 3. 积分（Explicit Euler）
        self.theta_dot_state += -(dt / epsilon) * sigr_val
        self.lambda_state    += (dt / epsilon) * (J @ self.theta_dot_state - xdot_c)  # Eq.23b
        return self.theta_dot_state.copy()

    def freeze(self):
        """cmd_valid=0 时调用，冻结状态不积分"""
        pass   # 什么都不做，state 保持不变
```

**禁止**：每 tick 调用 `np.linalg.solve`、`lstsq`、`scipy.optimize`、`osqp`——这些是 IK/QP，不是 RNN reproduction。Pinocchio 只提供 `J(q)`，不做任何求逆。也禁止旧错误式 `theta_dot_state - J.T @ lambda_state` 作为 `P_Ω` 输入。

---

### MF-2：定向误差必须用 Eq.(13) 四元数公式，不能用叉积近似

**问题**：现有 bridge（`compute_step4e_values`）用叉积近似：

```python
orientation_axis = cross3(tcp_z_axis_b, orientation_target_axis_b)   # 近似，不是论文
```

**论文要求**：Eq.(13) `e_qua = Q_d^{-1} ⊗ Q`，转换为 3D 角速度误差后 `ẋ_o = k_o e_o`（Eq.14）。

**必须改法**：

```python
# Step 1: 从 R_d 构造期望四元数 Q_d
Q_d = rotation_matrix_to_quaternion(R_d)           # R_d 来自 Eq.(10)

# Step 2: 从 RTDE 旋转向量构造当前四元数 Q
R_cur = rotvec_to_matrix(rx, ry, rz)               # RTDE pose[3:6]
Q_cur = rotation_matrix_to_quaternion(R_cur)

# Step 3: 四元数误差 Eq.(13)
e_qua = quaternion_multiply(quaternion_inverse(Q_d), Q_cur)

# Step 4: 转换为 3D 角速度误差（虚部 = 轴×sin(θ/2)）
e_o = e_qua[1:4]   # 虚部，当误差小时近似为 (θ/2)*轴
if e_qua[0] < 0:
    e_o = -e_o      # 处理 quaternion double cover

# Step 5: Eq.(14)
xdot_o = k_o * e_o   # k_o = 5（论文）
```

`R_d` 来自 Eq.(10)，由 `u = F_b/‖F_b‖` 构造（见 MF-3）。叉积近似可保留为 diagnostic 对比，但不得作为 xdot_o 的来源。

---

### MF-3：外环 ẋ_p 必须用完整 Φ_O/Φ̃_O 矩阵构造

**问题**：现有 bridge 的 `base_motion + force_cmd` 是标量投影近似，不含 Eq.(7) 的 Φ_O 矩阵。

**论文要求**：Eq.(7)(8)(16)(17) 完整链：

```python
# u = F/||F||，Eq.(9)
F_b = R_tcp2base @ F_tcp_zeroed         # base frame force
u   = F_b / np.linalg.norm(F_b)        # force-derived normal

# R_d，Eq.(10)
S   = skew_matrix(u)                    # 3x3 cross-product matrix
R_d = np.eye(3) + np.sin_u * S + (1 - np.cos_u) * S @ S
# 其中 sin_u = sin(arccos(clamp(dot(u, z_ref), -1, 1))) 等，
# 或直接用罗德里格斯公式从 u 构造 R_d（使 R_d z轴 = u）

# Φ_O / Φ̃_O，Eq.(7)
Phi_E       = np.diag([1.0, 1.0, 0.0])   # 运动在 EE x-y 平面
Phi_tilde_E = np.eye(3) - Phi_E
Phi_O       = R_d.T @ Phi_E              # 投影到世界切面
Phi_tilde_O = R_d.T @ Phi_tilde_E        # 投影到法向

# 外环 ẋ_p，Eq.(16/17)
e_p = x_pd - x_p                        # 位置误差（3D）
e_f = f_d - dot(F_b, u)                 # 沿法向的力误差
force_integral += e_f * dt
force_integral  = clamp(force_integral, -integral_limit, +integral_limit)

motion_component = Phi_O @ (xdot_pd + k_p * e_p)
force_component  = Phi_tilde_O @ ((e_f + k_f * force_integral) / M_d_scalar
                                  - (B_d_scalar / M_d_scalar) * xdot_p_prev_normal)
xdot_p = motion_component + force_component   # 3D linear

# xdot_c 拼接，Eq.(20b)
xdot_c = np.concatenate([xdot_p, xdot_o])    # 6D
```

**注意**：`M_d_scalar=12`, `B_d_scalar=550`（论文参数），`k_p=4`, `k_f=1`。

---

## 三、UR10e 6DOF 与论文 Franka 7DOF 的结构差异

**明确标注为 Deviation，不得声称严格复现此点：**

| 项目 | 论文（Franka 7DOF） | UR10e 6DOF |
|---|---|---|
| J(θ) 维度 | R^{6×7}，冗余 | R^{6×6}，方阵 |
| min ‖θ̇‖² 意义 | 冗余空间内最优 | 无活跃边界时退化为 J^{-1}ẋ_c |
| RNN 必要性 | 最优化 + 边界约束 | **仅边界约束**（但仍有价值） |

**RNN 在 6DOF 下仍然有价值的原因**：
1. 边界约束 ω^- ≤ θ̇ ≤ ω^+ 活跃时，QP 仍然非平凡
2. 有限时间收敛保证（DLS/IK 没有）
3. 无显式矩阵求逆，奇异处行为不同于 DLS
4. Lagrange 乘子 λ 状态携带约束满足信息

**文档化标注**：`# DEVIATION: non-redundant 6DOF; min-norm objective trivial when bounds inactive`

---

## 四、Bounds 实现（与论文精确一致）

Eq.(18-19)(20c)：

```python
omega_minus = np.maximum(alpha * (q_min - q), qdot_min)   # Eq.(19)
omega_plus  = np.minimum(qdot_max, alpha * (q_max - q))   # Eq.(18)

# qdot_max = qdot_min = ±0.15 rad/s（论文参数，与 step5d table 一致）
# q_min/q_max = UR10e 关节限位（需从 URDF 读取）
# alpha = ??? 见 Open Questions
```

`P_Ω(v) = np.clip(v, omega_minus, omega_plus)`，element-wise，不是矩阵投影。

---

## 五、sigr 实现

```python
def sigr(x: np.ndarray, r: float) -> np.ndarray:
    """Eq.(23a), Remark 5: sigr(x) = |x|^r * sign(x)"""
    return np.abs(x) ** r * np.sign(x)
```

- r ∈ (0, 1)，论文建议 r=0.2 收敛最快（Fig.5b）
- **禁止**用 `np.clip`、`np.tanh`、arctan 替代
- r 值最终需从论文 Section VI 参数表确认

---

## 六、Force Normal 策略（论文优先，正确）

**论文 Remark 6 原文**（Section VI）：  
"no filter was added to filter the force sensor signals"

**结论**：raw force-derived normal（u = F_b/‖F_b‖）是严格复现的正确选择。

| 用途 | 允许 | 禁止 |
|---|---|---|
| RNN 的 u / R_d 输入 | raw force normal | v31 filtered_live normal |
| Safety guard 监控 | EMA 轻量滤波（需标注 deviation） | 替代 RNN 输入 |
| 记录规则 | 若用任何滤波必须标 "Reproduction Deviation: safety-only" | 不得重标为 paper-faithful |

---

## 七、Open Questions（不能脑补，必须从 PDF 或实验坐标核实）

### OQ-1：Force sign convention（最高优先级，阻断接触段）

- Kunwei 传感器在 TCP 坐标输出，零漂后旋转至 base frame 得到 `F_b`
- 当 TCP 向下压表面时，`F_b[z]` 是正还是负？
- `u = F_b/‖F_b‖` 的方向（指向表面内法向 vs. 外法向）需与论文 R_d 期望方向一致
- **必须在有实际接触状态下测量，核实后才能开启接触段**

### OQ-2：α 逃逸速度增益（Eq.18-19）

- 论文说 "α > 0 is a constant"，但仿真和实验节均未列出具体数值
- 需从论文 Section V 仿真参数或 Appendix 查找
- 不能估填，alpha 直接影响边界约束的安全性

### OQ-3：r 参数（sigr 指数）

- 论文 Fig.5 显示 r=0.2 收敛最快但初期有振荡，r=0.8 稳定但慢
- Section VI 实验参数表是否有明确 r 值？需核实

### OQ-4：Eq.(17) 中的通信延迟 T

- 离散化时 T 是否等于 RTDE dt（=0.002s at 500Hz）？
- 论文提到 "system communication of robot controller"，需确认含义

---

## 八、Diagnostics（必须暴露，以证明不是 DLS/IK）

`StrictTaseRnnSolver.step()` 的返回值或 side-channel 必须包含：

```python
{
    "theta_dot_state":     np.ndarray(6),   # RNN θ̇_c 状态
    "lambda_state":        np.ndarray(6),   # Lagrange 乘子状态
    "proj_input_form":     "J.T @ lambda_state",
    "proj_input":          np.ndarray(6),   # J^T λ，clip 前
    "sigr_arg":            np.ndarray(6),   # sig^r 的输入
    "sigr_val":            np.ndarray(6),   # sig^r 的输出
    "projected":           np.ndarray(6),   # P_Omega clip 后结果
    "omega_minus":         np.ndarray(6),   # 实时下界
    "omega_plus":          np.ndarray(6),   # 实时上界
    "active_bounds_mask":  np.ndarray(6),   # bool，哪些关节 bound 活跃
    "constraint_residual": float,           # ‖Jθ̇_c − ẋ_c‖，越小越好
    "xdot_c":             np.ndarray(6),   # 外环输入（供 audit 对比）
}
```

写入 CSV debug 列（`_step5d_*` 前缀），与 `_step5c_cmd_qd*` 等字段并排记录。

**lambda_state 存在且随时间演化是最强的"非 DLS"证据**；DLS solver 不产生此量。

---

## 九、Discrete Integration 规则

论文给连续时间 ODE（Eq.23），paper-faithful 离散化用 **Explicit Euler**：

```python
# ε = 0.022（论文），dt = 0.002s（500Hz），dt/ε ≈ 0.091
# 每步系数约 9%；稳定性必须用离线数值 sanity 单独证明

theta_dot_state += -(dt / epsilon) * sigr_val      # Eq.(23a)
lambda_state    += (dt / epsilon) * constraint_res  # Eq.(23b)，constraint_res = J@θ̇_c − ẋ_c
```

当前实现按 printed Eq.(23) 使用显式 Euler；非零命令/非零初值的收敛不能由
单元测试脑补，必须作为后续 numeric sanity gate 单独关闭。若切换
semi-implicit 或其他积分器，必须标注为 deviation。

---

## 十、Test Requirements

建立 `tests/test_step5d_strict_rnn_solver.py`，以下测试必须全部通过才能开启任何 live gate：

| 测试 ID | 验证内容 | 通过条件 |
|---|---|---|
| T1 | lambda_state 跨 tick 持久 | 连续调用后 lambda_state 非零且不被每 tick 重置 |
| T2 | sigr 正确性 | `sigr(1.0, 0.5)==1.0`, `sigr(-2.0,0.2)==-(2^0.2)`, 不等于 clip 结果 |
| T3 | P_Ω element-wise saturation | 超界输入 → 恰好等于 ω^± 而非伪逆结果 |
| T4 | Eq.(23) 零状态不动点 | J=I, xdot_c=0, 初始 θ̇_state=0, λ_state=0 时状态保持零 |
| T5 | bound 投影正确 | `projected=P_Ω(J.T@lambda_state)` element-wise 不超界 |
| T6 | 无 DLS/IK 静态特征 | solver step 源码不含 pinv/lstsq/solve/scipy/osqp/DLS |
| T7 | 四元数定向误差精度 | 已知 R_d 和 R_cur → e_o 误差 < 1e-6 rad |
| T8 | Jacobian 符号验证 | J(q)@actual_qd（Pinocchio）vs actual_TCP_speed（RTDE），RMS < 1e-5 |
| T9 | stale guard 冻结 | cmd_valid=0 时 theta_dot_state 和 lambda_state 不变 |
| T10 | paper_truth gate 通过 | 清空所有 pending_pdf_verify 后 StrictTaseRnnSolver() 不抛异常 |

---

## 十一、Recommended Mapping Summary

以下映射可直接写入 Step5d spec，供实现参考：

```
A. Joint input:
   θ      = actual_q[0..5]           RTDE, rad
   θ̇_meas = actual_qd[0..5]          RTDE, rad/s，仅用于 diagnostic

B. Kinematics backend:
   J(q) = Pinocchio base→tool0+TCP_offset Jacobian (6×6)
   hash = calib_7367377276742883610
   [禁止 MuJoCo nominal model，禁止 DLS/pseudo-inverse]

C. Force normal（论文优先）:
   F_t  = kunwei zeroed force in TCP frame
   F_b  = R_tcp2base @ F_t
   u    = F_b / ||F_b||              Eq.(9)，sign 待实测核实（OQ-1）
   R_d  = I + sin(u)S + (1−cos(u))S²  Eq.(10)

D. Outer loop xdot_p（Eq.16/17）:
   Phi_O       = R_d.T @ Diag(1,1,0)   Eq.(7)
   Phi_tilde_O = R_d.T @ Diag(0,0,1)
   e_p         = x_pd − x_p
   e_f         = f_d − dot(F_b, u)
   force_integral += e_f * dt  (clipped)
   xdot_p = Phi_O @ (xdot_pd + k_p*e_p)
            + Phi_tilde_O @ ((e_f + k_f*force_integral)/M_d − (B_d/M_d)*xdot_p_normal_prev)
   [M_d=12, B_d=550, k_p=4, k_f=1]

E. Outer loop xdot_o（Eq.13/14）:
   Q_d    = rot2quat(R_d)
   Q_cur  = rot2quat(rotvec2mat(TCP_pose[3:6]))
   e_qua  = quat_inv(Q_d) ⊗ Q_cur
   e_o    = e_qua[1:4]  (虚部，处理 double cover)
   xdot_o = k_o * e_o   [k_o=5]

F. RNN input:
   xdot_c = [xdot_p (3D); xdot_o (3D)]   6D

G. Bounds（Eq.18-19）:
   omega_minus[i] = max(alpha*(q_min[i]−q[i]), −0.15)
   omega_plus[i]  = min(0.15, alpha*(q_max[i]−q[i]))
   [alpha 待 PDF 核实（OQ-2）]

H. RNN inner loop（Eq.23，stateful，ε=0.022）:
   # 每 tick（dt=0.002s）：
   proj_input = J.T @ λ_state
   proj    = clip(proj_input, ω⁻, ω⁺)
   sigr_v  = |θ̇_state − proj|^r * sign(θ̇_state − proj)
   θ̇_state += −(dt/ε) * sigr_v            # Eq.(23a)
   λ_state  += (dt/ε) * (J@θ̇_state − xdot_c)  # Eq.(23b)
   # cmd_valid=0 时两个状态均冻结
   # [禁止] proj_input = θ̇_state − J.T @ λ_state

I. Output（registers 37..47）:
   qd0..qd5   = θ̇_state → registers 37..42
   cmd_valid  → register 43
   path_time  → register 44
   force_error_n → register 45
   orientation_error → register 46
   solver_status → register 47
```

---

## 十二、Step5d Completion Gates 状态

以下 gate 中，`✗` 表示未完成，`?` 表示信息不足：

| Gate | 状态 | 阻断原因 |
|---|---|---|
| paper_truth JSON 无 pending_pdf_verify | ✗ | OQ-1/2/3/4 未核实 |
| StrictTaseRnnSolver 有状态实现 | ✓ | offline Eq.(23) body 已实现；live entrypoint 仍 blocked |
| 四元数定向误差路径 | ✓ | offline `step5d_paper_outer_loop.py` 实现 Eq.(13)/(14)；bridge live 未接入 |
| 完整 Phi_O 外环 | ✓ | offline `step5d_paper_outer_loop.py` 实现 Eq.(7)/(8)/(16)/(17)；force sign/T 仍需关闭 |
| calibrated Pinocchio Jacobian audit pass | ✓ | `runs/step5c_calibrated_kinematics_audit_20260613_003314` 已通过 |
| qdot register path proof 37..47 | ✓ | 已有 offline 测试 |
| structural full-chain sanity | ✓ | `runs/step5d_numeric_sanity_20260614_214203`；非 contact evidence |
| production/live numeric sanity | ✗ | force sign、nominal qdot bound、contact route 尚未关闭 |
| T1-T10 测试全通过 | ✗ | strict RNN core、outer-loop、structural chain 已有；live/contact gates 尚未完成 |
| 新 non-quarantine Step5d TP package | ✗ | 依赖上面所有 gate |
| controller read-back SHA 验证 | ✗ | 依赖 package |
| 独立 live dry-run plan 明确接受 | ✗ | 最后 gate |

**旧 Step5c DLS/quarantine 路线的任何文件不得作为上述 gate 的完成证据。**

---

*审查基于论文 PDF 全文（10页）、config/step5_stage_table.json、config/step5c_tase_paper_truth.json、STEP5_FLOW.md、tools/step5c_strict_rnn.py、tools/kunwei_rtde_bridge.py、tests/test_step5c_joint.py 综合分析。*

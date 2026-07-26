# UR5e EOAT print v2

当前版本是探针式接触头的概念版，包含：

- `source/ur5e_probe_concept_v2.py`
- `parts/ur5e_probe_concept_v2_base.*`
- `parts/ur5e_probe_concept_v2_probe.*`
- `parts/ur5e_probe_concept_v2_fitcheck.*`
- `assembly/ur5e_probe_concept_v2_assembly.*`
- `assembly/ur5e_probe_concept_v2_onepiece.*`
- `preview/ur5e_probe_concept_v2_side.png`

## 当前目的

- 验证 `UR5e` 裸法兰接口方向是否正确
- 先用 `fit-check` 验证件挡掉整件报废风险
- 在接口确认前，不批准直接打印 `onepiece`

## 当前高风险参数

- `robot_pattern_pcd_mm = 50.0`
- `robot_screw_clearance_mm = 6.6`
- `robot_socket_head_counterbore_mm = 11.0`
- `robot_socket_head_cbore_depth_mm = 6.0`
- `robot_center_recess_mm = 31.7`
- `adapter_thickness_mm = 12.0`

## 当前默认结论

- 机器人侧接口方向按 `ISO 9409-1:2004 [50-4-M6 Type]` 思路建模
- 现有模型大概率方向正确，但尚未获准直接整件打印上机
- 第一打印件必须是 `fit-check`

## 打印前顺序

1. 拆下 `RG2`
2. 拍裸法兰照片，带普通尺子同框
3. 静态复核照片
4. 先试 `fit-check`
5. `fit-check` 通过后，再考虑 `base` 或 `onepiece`

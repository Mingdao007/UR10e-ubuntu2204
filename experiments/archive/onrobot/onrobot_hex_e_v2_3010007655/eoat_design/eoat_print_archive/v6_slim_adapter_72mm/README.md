# UR5e flat-pad tool v6 slim adapter, 72 mm OD

这个版本继承 v4 的 flat-pad contact tool，但把近端 adapter 外径从 80.0 mm 缩到 72.0 mm。
目标不是避开 wrist 自碰，而是让近端外形更收、更接近当前装配视觉，并增加线缆或外部结构 clearance。

## 当前判断

- 这是更接近 paper 风格的 `UR5e`（Universal Robots 5e）直连概念版
- 底部接触件是平底圆形垫，不是滚轮
- 机器人侧仍然保留 4 x M6 on 50 mm PCD 的法兰孔位
- 中心 register/recess 继续保留为定位/避让特征
- OnRobot QC-R 上的 smooth locating pin 不复制到这个 direct-flange 版本
- 如果以后需要真实软垫、海绵垫或打磨片，可以把当前 `pad` 当成载体几何继续改

## 输出文件

- `ur5e_flat_pad_tool_v6_slim_adapter_body.step / .stl`
- `ur5e_flat_pad_tool_v6_slim_adapter_pad.step / .stl`
- `ur5e_flat_pad_tool_v6_slim_adapter_fitcheck.step / .stl`
- `ur5e_flat_pad_tool_v6_slim_adapter_assembly.step / .stl`
- `ur5e_flat_pad_tool_v6_slim_adapter_onepiece.step / .stl`

## 主要尺寸

- Adapter 外径：72.0 mm
- 上部主体外径：40.0 mm
- 下部圆柱外径：30.0 mm
- 接触垫底面外径：30.0 mm
- 接触垫高度：8.0 mm
- 接触面距法兰面轴向距离：96.0 mm
- Counterbore 外缘到 adapter 外缘的径向余量：5.5 mm

## 说明

- `onepiece` 适合快速看整体比例或直接打一体件
- 分件版 `body + pad` 更适合后面改单独接触垫
- 机器人侧接口仍沿用前一版的 `50-4-M6`
- 如果后续上真实 force-contact，先实测中心 register/recess 和 screw seating

# UR5e flat-pad tool v4

这个版本不再走滚轮路线，而是按论文图和你刚发的参考图，收成更像原文的粗直筒 + 平底圆形接触垫。

## 当前判断

- 这是更接近 paper 风格的 `UR5e`（Universal Robots 5e）直连概念版
- 底部接触件是平底圆形垫，不是滚轮
- 如果以后需要真实软垫、海绵垫或打磨片，可以把当前 `pad` 当成载体几何继续改

## 输出文件

- `ur5e_flat_pad_tool_v4_body.step / .stl`
- `ur5e_flat_pad_tool_v4_pad.step / .stl`
- `ur5e_flat_pad_tool_v4_fitcheck.step / .stl`
- `ur5e_flat_pad_tool_v4_assembly.step / .stl`
- `ur5e_flat_pad_tool_v4_onepiece.step / .stl`

## 主要尺寸

- 上部主体外径：40.0 mm
- 下部圆柱外径：30.0 mm
- 接触垫底面外径：30.0 mm
- 接触垫高度：8.0 mm
- 接触面距法兰面轴向距离：96.0 mm

## 说明

- `onepiece` 适合快速看整体比例或直接打一体件
- 分件版 `body + pad` 更适合后面改单独接触垫
- 机器人侧接口仍沿用前一版的 `50-4-M6`

# UR5e EOAT print v1

版本目录：

- `source/` 参数化建模脚本
- `parts/` 单独可打印零件
- `assembly/` 当前版本没有总装输出
- `preview/` 预览图
- `notes/` 假设与说明

当前版本先只落地最小可打印零件：

- `source/ur5e_contact_adapter_v1.py`
  参数化建模脚本
- `parts/ur5e_contact_adapter_v1.stl`
  3D 打印网格
- `parts/ur5e_contact_adapter_v1.step`
  可继续进 CAD 的交换格式
- `preview/ur5e_contact_adapter_v1_layout.png`
  正反面孔位示意

## 当前结构

- 机器人侧：`UR5e` 法兰转接盘
- 工具侧：通用三孔安装面
- 中心：预留通孔，方便后续给接触头、弹簧件、线缆或小轴穿过

## v1 参数

- 外径：`80 mm`
- 厚度：`12 mm`
- 机器人侧孔型：`4 x M6 clearance` on `50 mm PCD`
- 机器人侧沉孔：`11 mm` 直径，`6 mm` 深
- 机器人侧中心退刀/定位凹槽：`31.7 mm` 直径，`2 mm` 深
- 工具侧三孔：`3 x 5.5 mm clearance` on `36 mm PCD`
- 中心通孔：`20 mm`

## 说明

- `50 mm PCD + 4 x M6` 依据 `UR5e` 使用的 `ISO 9409-1-50-4-M6` 风格接口来建。
- `31.7 mm` 中心凹槽尺寸目前按常见 `50-4-M6` 转接件经验值先做，不当作最终定版尺寸。
- 真正打印前，建议你用卡尺量一次机器人法兰：
  - 四孔节圆直径
  - 中心定位台阶或配合区直径
  - 可接受的螺钉头高度

## 下一步

下一版建议二选一：

1. 在这个转接盘前面接一个 `spring-loaded` 接触头
2. 在这个转接盘前面接一个可换的打磨头/圆头夹具模块

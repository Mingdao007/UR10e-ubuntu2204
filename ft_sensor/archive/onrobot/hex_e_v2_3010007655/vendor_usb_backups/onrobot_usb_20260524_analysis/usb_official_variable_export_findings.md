# OnRobot USB 官方内容审查记录

日期：2026-05-24

## 备份位置

- 原始 U 盘挂载点：`/media/andy/ONROBOT`
- 本地备份：
  `/home/andy/ur10e_ros2_ws/ft_sensor/archive/onrobot/hex_e_v2_3010007655/vendor_usb_backups/onrobot_usb_20260524`
- 文件清单：
  `/home/andy/ur10e_ros2_ws/ft_sensor/archive/onrobot/hex_e_v2_3010007655/vendor_usb_backups/onrobot_usb_20260524_file_manifest.tsv`
- 备份规模：`483` 个普通文件，`414M`

## 已审查的官方文件

- `OnRobot_UR_Programs/FT-OnRobot-4.1.7.1754.urcap`
- `OnRobot_UR_Programs/*.urp`
- `Interfaces and Softwares/Ethernet/Example/tcp.c`
- `Interfaces and Softwares/Ethernet/Example/highspeed_udp.c`
- `Interfaces and Softwares/Ethernet/Manual/OnRobot Compute Box Robot Controller Interface Description_E7.pdf`
- `HEX User Manual for UR/E14/User Manual for the UR HEX Sensor Kit_E14_en.pdf`

## 关键结论

1. 官方 URCap 脚本确认 PolyScope `Variables` 页上的 `Fx`、`Fy`、`Fz`、
   `Tx`、`Ty`、`Tz`、`F3D`、`T3D`、`bFT`、`tFT` 是 URScript 变量。

2. `hex.globals.script` 定义了这些全局变量：

   ```text
   global Fx=0
   global Fy=0
   global Fz=0
   global Tx=0
   global Ty=0
   global Tz=0
   global F3D=0
   global T3D=0
   global bFT=[0,0,0,0,0,0]
   global tFT=[0,0,0,0,0,0]
   FT_hex=[0,0,0,0,0,0]
   ```

3. `hex.engine.script` 从 `FT_hex` 经过 TCP 变换计算出可见变量，并写入
   `tFT`：

   ```text
   Fx=tcpF[0]
   Fy=tcpF[1]
   Fz=tcpF[2]
   Tx=tcpT[0]
   Ty=tcpT[1]
   Tz=tcpT[2]
   F3D=sqrt(Fx*Fx+Fy*Fy+Fz*Fz)
   T3D=sqrt(Tx*Tx+Ty*Ty+Tz*Tz)
   bFT=[bF[0],bF[1],bF[2],bT[0],bT[1],bT[2]]
   tFT=[Fx,Fy,Fz,Tx,Ty,Tz]
   ```

4. `hex.engine_start.script` 会启动内部向量处理线程并调用 `of_ft_bias()`。
   这支持当前判断：PolyScope 变量层和直接 TCP DAQ `READFT` 原始值不是同一
   个零点/补偿口径，不能只因为频率接近就把二者等价。

5. 在已解包的官方 `.urp` 示例和 URCap 脚本中，没有发现
   `write_output_*register`、`output_double_register`、
   `output_float_register`、`output_int_register` 或类似的现成 RTDE 输出寄存器
   映射。因此，U 盘没有提供可直接让 Ubuntu 读取 PolyScope Variables 的现成
   导出程序。

6. 官方 `.urp` 示例主要包含 `F/T Zero`、`F/T Move`、`F/T Waypoint`、
   `F/T Search`、`F/T Control` 等应用节点。它们是示例程序，不适合作为当前
   no-motion 采集程序运行。

## 当前推荐映射

继续采用手动 PolyScope no-motion 程序导出方案。用户在正在运行的
`wait 0.01` no-motion 程序里添加一个 Script 节点，让 URCap 变量写入 RTDE
输出寄存器；Ubuntu 只做读取。

推荐 6 轴映射：

```text
write_output_float_register(24, Fx)
write_output_float_register(25, Fy)
write_output_float_register(26, Fz)
write_output_float_register(27, Tx)
write_output_float_register(28, Ty)
write_output_float_register(29, Tz)
```

等价写法也可以从 `tFT` 导出：

```text
write_output_float_register(24, tFT[0])
write_output_float_register(25, tFT[1])
write_output_float_register(26, tFT[2])
write_output_float_register(27, tFT[3])
write_output_float_register(28, tFT[4])
write_output_float_register(29, tFT[5])
```

优先使用 `Fx` 到 `Tz` 的直接写法，因为官方脚本已确认它们是全局变量，且
和 PolyScope `Variables` 页命名一致。

## 安全边界

- 不运行 `urmagic_onrobot.sh`。
- 不运行官方 `.urp` 示例。
- 不执行 `F/T Zero`、bias、filter、speed、payload、TCP 或运动相关命令。
- Ubuntu 侧只读取 RTDE 输出寄存器；PolyScope 程序编辑和启动由用户手动完成。

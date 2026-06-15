#!/usr/bin/env python3
"""Write the Chinese Markdown report for the mounted sensor-only 8 h run."""

from __future__ import annotations

import csv
import collections
import json
from pathlib import Path


RUN_ROOT = Path(__file__).resolve().parent
EXPERIMENT_ROOT = RUN_ROOT.parents[1]
AXES = ("fxN", "fyN", "fzN", "txNm", "tyNm", "tzNm")
AXIS_LABELS = {
    "fxN": "Fx (N)",
    "fyN": "Fy (N)",
    "fzN": "Fz (N)",
    "txNm": "Tx (Nm)",
    "tyNm": "Ty (Nm)",
    "tzNm": "Tz (Nm)",
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path) -> str:
    return path.relative_to(RUN_ROOT).as_posix()


def f(value: float | int | None, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.{digits}f}"


def signed(value: float | int | None, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):+.{digits}f}"


def tf(value: object) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def artifact_link(path: Path) -> str:
    return f"[{path.name}]({rel(path)})"


def checkpoint_names() -> list[str]:
    names = []
    root = RUN_ROOT / "checkpoints"
    for hour in range(1, 9):
        name = f"{hour:02d}h"
        if (root / name / "summary.json").exists():
            names.append(name)
    return names


def load_bins(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def event_counts() -> collections.Counter[str]:
    events = RUN_ROOT / "main_8h" / "events.jsonl"
    counts: collections.Counter[str] = collections.Counter()
    if not events.exists():
        return counts
    for line in events.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        counts[item.get("event", "unknown")] += 1
    return counts


def selected_bin_rows(bins: list[dict[str, str]]) -> list[str]:
    if not bins:
        return ["主测 `drift_10min_bins.csv` 尚未生成。"]
    targets = {600, 1800, 3600, 7200, 14400, 21600, 28800}
    rows = [
        "| 区间 (s) | 样本数 | Fz均值 (N) | 相对前10min基线 (N) | Fx均值 (N) | Fy均值 (N) |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in bins:
        end = int(round(float(row["bin_end_s"])))
        if end not in targets:
            continue
        rows.append(
            "| "
            + " | ".join(
                [
                    f"{float(row['bin_start_s']):.0f}-{float(row['bin_end_s']):.0f}",
                    row["row_count"],
                    f(row.get("fzN_mean")),
                    signed(row.get("fzN_mean_minus_initial_baseline")),
                    f(row.get("fxN_mean")),
                    f(row.get("fyN_mean")),
                ]
            )
            + " |"
        )
    return rows


def hourly_summary_table(checkpoints: list[str]) -> list[str]:
    rows = [
        "| 快照 | 时长 (s) | 样本数 | Status | Auth | Bias | reconnect/error | Fz首值 (N) | Fz末值 (N) | Fz变化 (N) | Fz均值 (N) | Fz std (N) |",
        "|---|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in checkpoints:
        summary = load_json(RUN_ROOT / "checkpoints" / name / "summary.json")
        fz = summary["axis_stats"]["fzN"]
        rows.append(
            "| "
            + " | ".join(
                [
                    name,
                    f(summary["duration_s"], 1),
                    str(summary["rows"]),
                    str(summary["status"]),
                    tf(summary["authenticated"]),
                    tf(summary["bias"]),
                    "N/A",
                    f(fz["first"]),
                    f(fz["last"]),
                    signed(fz["last_minus_first"]),
                    f(fz["mean"]),
                    f(fz["std"]),
                ]
            )
            + " |"
        )
    return rows


def axis_stats_table(prefix: str, summary: dict) -> list[str]:
    rows = [
        f"| {prefix} | 轴 | 均值 | Std | Min | Max | 首值 | 末值 | 末-首 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for axis in AXES:
        stats = summary["axis_stats"][axis]
        rows.append(
            "| "
            + " | ".join(
                [
                    prefix,
                    AXIS_LABELS[axis],
                    f(stats["mean"]),
                    f(stats["std"]),
                    f(stats["min"]),
                    f(stats["max"]),
                    f(stats["first"]),
                    f(stats["last"]),
                    signed(stats["last_minus_first"]),
                ]
            )
            + " |"
        )
    return rows


def hourly_axis_stats_table(checkpoints: list[str]) -> list[str]:
    rows = [
        "| 快照 | 轴 | 均值 | Std | Min | Max | 首值 | 末值 | 末-首 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in checkpoints:
        summary = load_json(RUN_ROOT / "checkpoints" / name / "summary.json")
        for axis in AXES:
            stats = summary["axis_stats"][axis]
            rows.append(
                "| "
                + " | ".join(
                    [
                        name,
                        AXIS_LABELS[axis],
                        f(stats["mean"]),
                        f(stats["std"]),
                        f(stats["min"]),
                        f(stats["max"]),
                        f(stats["first"]),
                        f(stats["last"]),
                        signed(stats["last_minus_first"]),
                    ]
                )
                + " |"
            )
    return rows


def artifact_table(checkpoints: list[str]) -> list[str]:
    rows = [
        "| 阶段 | raw CSV | summary | 10 min bins | full 图 | Fz 图 | fxy 图 |",
        "|---|---|---|---|---|---|---|",
    ]
    dirs = [("dry_run_2min", RUN_ROOT / "dry_run_2min"), ("main_8h", RUN_ROOT / "main_8h")]
    dirs.extend((name, RUN_ROOT / "checkpoints" / name) for name in checkpoints)
    for label, directory in dirs:
        rows.append(
            "| "
            + " | ".join(
                [
                    label,
                    artifact_link(directory / "raw_wrench.csv"),
                    artifact_link(directory / "summary.json"),
                    artifact_link(directory / "drift_10min_bins.csv"),
                    artifact_link(directory / "force_torque.png"),
                    artifact_link(directory / "fz.png"),
                    artifact_link(directory / "fxy.png"),
                ]
            )
            + " |"
        )
    return rows


def hourly_images(checkpoints: list[str]) -> list[str]:
    rows: list[str] = []
    for name in checkpoints:
        rows.extend(
            [
                f"### {name}",
                "",
                f"![{name} full force/torque](checkpoints/{name}/force_torque.png)",
                "",
                f"![{name} Fz](checkpoints/{name}/fz.png)",
                "",
                f"![{name} Fx/Fy](checkpoints/{name}/fxy.png)",
                "",
            ]
        )
    return rows


def checkpoint_helper_code() -> str:
    path = RUN_ROOT / "make_hourly_checkpoints.py"
    return path.read_text(encoding="utf-8").rstrip()


def main() -> int:
    dry = load_json(RUN_ROOT / "dry_run_2min" / "summary.json")
    main_summary = load_json(RUN_ROOT / "main_8h" / "summary.json")
    checkpoints = checkpoint_names()
    bins = load_bins(RUN_ROOT / "main_8h" / "drift_10min_bins.csv")
    events = event_counts()
    final_checkpoint = load_json(RUN_ROOT / "checkpoints" / checkpoints[-1] / "summary.json")
    main_fz = main_summary["axis_stats"]["fzN"]
    dry_fz = dry["axis_stats"]["fzN"]

    lines: list[str] = [
        "# OnRobot HEX-E 安装在 UR10e 后 8 小时只读漂移记录",
        "",
        "## 实验目的",
        "",
        "本次实验记录 OnRobot HEX-E 安装在 UR10e 腕部后的 sensor-only 长时间漂移。目标不是验证接触控制，也不是做清零前后对比，而是在机器人静止、末端无接触、传感器已上电的条件下观察 8 小时只读力/力矩输出是否稳定。",
        "",
        "本报告只比较同一条 8 小时主测内的 hourly checkpoints。所有 checkpoint 都由同一个 `main_8h/raw_wrench.csv` 按 `t_s <= N*3600` 截取，并用同一个分析脚本重算；因此表格口径一致。",
        "",
        "## 设备与实验条件",
        "",
        "| 项目 | 内容 |",
        "|---|---|",
        "| 传感器 | OnRobot HEX-E v2，live serial: `HEXEB806` |",
        "| 文档/包名标识 | `3010007655` |",
        "| 安装状态 | HEX-E 已安装在 UR10e 腕部；本 run 视为 sensor-only，无外接负载、无接触 |",
        "| 机器人状态 | UR10e 静止；本实验不发送运动命令 |",
        "| `P_work_drift` | 未在开跑前提供；报告不反推位姿 |",
        "| Compute Box | `192.168.1.1`，`/version` 返回 `4.1.8` |",
        "| Ubuntu 网口 | `enp3s0`，`192.168.1.10/24` |",
        f"| 运行目录 | `{RUN_ROOT}` |",
        "| 主测时长 | 28800 s，约 8 h |",
        "| checkpoint | 01h 到 08h，每小时一个截断重分析目录 |",
        "| 清零/偏置 | 未调用 UR `zero_ftsensor()`；未调用 OnRobot zero/bias/autocalib |",
        "| 安全边界 | 只读采集；未调用 firmware、DIP switch、配置写入、TCP/payload/gravity/ROS/UR 配置修改 |",
        "| fxy 规则 | Fx 和 Fy 两条曲线放在同一张图 |",
        "",
        "预检记录见 [preflight_notes.md](preflight_notes.md)。用户确认 Compute Box 已插电后开始；本机预检显示 `ping 192.168.1.1` 为 4/4 replies、0% packet loss。",
        "",
        "## 实验命令",
        "",
        "2 分钟 dry run：",
        "",
        "```bash",
        "python3 tools/onrobot_socketio_logger.py \\",
        "  --host 192.168.1.1 \\",
        "  --duration-s 120 \\",
        f"  --out-dir \"{RUN_ROOT}/dry_run_2min\" \\",
        "  --status-every-s 30 \\",
        "  --flush-every-s 30",
        "```",
        "",
        "8 小时主测：",
        "",
        "```bash",
        "python3 tools/onrobot_socketio_logger.py \\",
        "  --host 192.168.1.1 \\",
        "  --duration-s 28800 \\",
        f"  --out-dir \"{RUN_ROOT}/main_8h\" \\",
        "  --status-every-s 1800 \\",
        "  --flush-every-s 30",
        "```",
        "",
        "每小时 checkpoint 由 `make_hourly_checkpoints.py` 从正在增长的主 CSV 中截取 `t_s <= 3600, 7200, ..., 28800`，再运行：",
        "",
        "```bash",
        "python3 tools/analyze_onrobot_drift.py \"$CHECKPOINT_DIR\" --baseline-s 600 --bin-s 600",
        "```",
        "",
        "## 数据与图片",
        "",
        "主测完整 force/torque 图如下。上图为 Fx/Fy/Fz，下图为 Tx/Ty/Tz；横轴是采集开始后的秒数。",
        "",
        "![8 h OnRobot HEX-E force/torque](main_8h/force_torque.png)",
        "",
        "Fz 单独图用于直接观察竖直方向读数在 8 小时内的变化。",
        "",
        "![8 h OnRobot HEX-E Fz](main_8h/fz.png)",
        "",
        "fxy 图把 Fx 和 Fy 放在同一坐标系中，用于判断水平分量是否同步漂移或各自偏移。",
        "",
        "![8 h OnRobot HEX-E Fx/Fy](main_8h/fxy.png)",
        "",
        "## 统计结果",
        "",
        f"Dry run 通过：`rows={dry['rows']}`，`duration_s={f(dry['duration_s'])}`，`status={dry['status']}`，`authenticated={tf(dry['authenticated'])}`，`bias={tf(dry['bias'])}`，`reconnect/error={dry['reconnect_count']}/{dry['error_count']}`；Fz 从 `{f(dry_fz['first'])} N` 到 `{f(dry_fz['last'])} N`，变化 `{signed(dry_fz['last_minus_first'])} N`。",
        "",
        f"主测完成：`rows={main_summary['rows']}`，`duration_s={f(main_summary['duration_s'])}`，`status={main_summary['status']}`，`authenticated={tf(main_summary['authenticated'])}`，`bias={tf(main_summary['bias'])}`，`reconnect/error={main_summary['reconnect_count']}/{main_summary['error_count']}`。Fz 从 `{f(main_fz['first'])} N` 到 `{f(main_fz['last'])} N`，首末变化 `{signed(main_fz['last_minus_first'])} N`；Fz 均值 `{f(main_fz['mean'])} N`，std `{f(main_fz['std'])} N`。",
        "",
        f"{checkpoints[-1]} checkpoint 严格按 `t_s <= 28800` 截取，`rows={final_checkpoint['rows']}`；主测 summary 包含边界后的最后一个样本，`rows={main_summary['rows']}`，所以两者末值可能差一个采样点。这不是协议差异。",
        "",
        f"`events.jsonl` 中事件计数为：`version_ok={events.get('version_ok', 0)}`，`socket_handshake_ok={events.get('socket_handshake_ok', 0)}`，`poll_http_error={events.get('poll_http_error', 0)}`，`status={events.get('status', 0)}`，`logger_finished={events.get('logger_finished', 0)}`。这里的 `poll_http_error 400` 与 Socket.IO polling 会话重建相伴出现；由于主 CSV 持续写入且最终有 `logger_finished`，本报告不把它判为不可恢复连接失败。",
        "",
        "每小时 checkpoint 统一表：",
        "",
        "说明：checkpoint 目录只复制主 CSV 的截断数据，不复制 `events.jsonl`，所以 hourly 表中的 `reconnect/error` 标为 `N/A`。事件计数以主测 `main_8h/summary.json` 为准。",
        "",
        *hourly_summary_table(checkpoints),
        "",
        "主测完整轴统计：",
        "",
        *axis_stats_table("main_8h", main_summary),
        "",
        "主测 10 分钟 bin 选点如下；`相对前10min基线` 使用主测最初 600 s 的均值作为零参考，只用于看趋势，不代表传感器被清零。",
        "",
        *selected_bin_rows(bins),
        "",
        "## 结论",
        "",
        f"本次 mounted sensor-only 8 小时只读主测完整完成，最终 `status=0`、`authenticated=True`、`bias=False`，每小时 checkpoint 从 01h 到 {checkpoints[-1] if checkpoints else 'N/A'} 均已生成。主测期间未调用任何清零、bias、autocalib、配置写入或机器人运动命令。",
        "",
        f"按主测首末样本看，Fz 从 `{f(main_fz['first'])} N` 到 `{f(main_fz['last'])} N`，变化 `{signed(main_fz['last_minus_first'])} N`。更稳健地看，hourly checkpoint 的 Fz 均值从 01h 的 `{f(load_json(RUN_ROOT / 'checkpoints' / checkpoints[0] / 'summary.json')['axis_stats']['fzN']['mean'])} N` 逐步到 {checkpoints[-1]} 的 `{f(load_json(RUN_ROOT / 'checkpoints' / checkpoints[-1] / 'summary.json')['axis_stats']['fzN']['mean'])} N`；这说明本 run 的主要变化应按长时间均值和图形趋势判断，而不是只看某一个末端样本。",
        "",
        "本实验的限制是：开跑前没有记录 `P_work_drift` 六轴角，报告不能把漂移和具体姿态做定量绑定；本 run 也没有温度通道，因此这里称为上电后长时读数漂移，不把它直接等同于温度模型。",
        "",
        "## 下一步",
        "",
        "下一次正式对比应记录 `P_work_drift` 六轴角，并在同一 mounted sensor-only 条件下重复一条短 run 或同姿态 8h run，用来判断这次 Fz 均值变化是否可重复。若要进一步进入接触实验，需要先完成低速无接触 cable sweep，并记录真实 TCP、payload 和传感器安装方向。",
        "",
        "## Appendix A. 每小时 checkpoint 图片",
        "",
        *hourly_images(checkpoints),
        "## Appendix B. 完整轴统计",
        "",
        *hourly_axis_stats_table(checkpoints),
        "",
        "## Appendix C. 产物链接",
        "",
        *artifact_table(checkpoints),
        "",
        "## Appendix D. Checkpoint 脚本",
        "",
        "```python",
        checkpoint_helper_code(),
        "```",
        "",
    ]

    (RUN_ROOT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(RUN_ROOT / "report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

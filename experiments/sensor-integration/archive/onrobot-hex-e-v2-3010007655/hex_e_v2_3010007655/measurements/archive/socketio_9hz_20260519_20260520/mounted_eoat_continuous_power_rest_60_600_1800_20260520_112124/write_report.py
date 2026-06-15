#!/usr/bin/env python3
"""Write the Chinese report for the mounted-EOAT continuous-power rest run."""

from __future__ import annotations

import collections
import csv
import json
from pathlib import Path


RUN_ROOT = Path(__file__).resolve().parent
SEGMENTS = ["S00_60s", "S01_600s", "S02_1800s"]
ABORTED_SEGMENT = "S02_3600s"
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


def f(value: object, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):.{digits}f}"


def signed(value: object, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):+.{digits}f}"


def tf(value: object) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def event_counts(segment: str) -> collections.Counter[str]:
    path = RUN_ROOT / "segments" / segment / "events.jsonl"
    counts: collections.Counter[str] = collections.Counter()
    if not path.exists():
        return counts
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        counts[item.get("event", "unknown")] += 1
    return counts


def protocol_events() -> list[dict]:
    path = RUN_ROOT / "protocol_events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def segment_summaries() -> dict[str, dict]:
    return {name: load_json(RUN_ROOT / "segments" / name / "summary.json") for name in SEGMENTS}


def aborted_summary() -> dict | None:
    path = RUN_ROOT / "segments" / ABORTED_SEGMENT / "ABORTED_TOUCHED.json"
    if not path.exists():
        return None
    return load_json(path)


def rest_summaries() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in ["R00_rest_30min", "R01_rest_30min"]:
        path = RUN_ROOT / "rests" / name / "rest_summary.json"
        if path.exists():
            out[name] = load_json(path)
    return out


def protocol_table(summaries: dict[str, dict], rests: dict[str, dict]) -> list[str]:
    rows = [
        "| Segment | Duration (s) | Samples | Power state | Rest before | Rest after | UR10e state | Status/Auth/Bias | reconnect/error |",
        "|---|---:|---:|---|---:|---:|---|---|---:|",
    ]
    rest_after = {
        "S00_60s": rests.get("R00_rest_30min", {}).get("actual_duration_s"),
        "S01_600s": rests.get("R01_rest_30min", {}).get("actual_duration_s"),
        "S02_1800s": None,
    }
    rest_before = {
        "S00_60s": None,
        "S01_600s": rests.get("R00_rest_30min", {}).get("actual_duration_s"),
        "S02_1800s": rests.get("R01_rest_30min", {}).get("actual_duration_s"),
    }
    for name in SEGMENTS:
        summary = summaries[name]
        rows.append(
            "| "
            + " | ".join(
                [
                    name,
                    f(summary["duration_s"], 1),
                    str(summary["rows"]),
                    "continuous_on",
                    f(rest_before[name], 1),
                    f(rest_after[name], 1),
                    "off_not_read",
                    f"{summary['status']}/{tf(summary['authenticated'])}/{tf(summary['bias'])}",
                    f"{summary['reconnect_count']}/{summary['error_count']}",
                ]
            )
            + " |"
        )
    return rows


def fz_table(summaries: dict[str, dict]) -> list[str]:
    rows = [
        "| Segment | Fz首值 (N) | Fz末值 (N) | 末-首 (N) | Fz均值 (N) | Std (N) | Min (N) | Max (N) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in SEGMENTS:
        stats = summaries[name]["axis_stats"]["fzN"]
        rows.append(
            "| "
            + " | ".join(
                [
                    name,
                    f(stats["first"]),
                    f(stats["last"]),
                    signed(stats["last_minus_first"]),
                    f(stats["mean"]),
                    f(stats["std"]),
                    f(stats["min"]),
                    f(stats["max"]),
                ]
            )
            + " |"
        )
    return rows


def axis_table(summaries: dict[str, dict]) -> list[str]:
    rows = [
        "| Segment | Axis | Mean | Std | Min | Max | First | Last | Last-First |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in SEGMENTS:
        for axis in AXES:
            stats = summaries[name]["axis_stats"][axis]
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


def selected_bins(segment: str) -> list[str]:
    path = RUN_ROOT / "segments" / segment / "drift_10min_bins.csv"
    if not path.exists():
        return [f"`{segment}` 没有 `drift_10min_bins.csv`。"]
    with path.open(newline="", encoding="utf-8") as handle:
        bins = list(csv.DictReader(handle))
    rows = [
        f"### {segment}",
        "",
        "| 区间 (s) | 样本数 | Fz均值 (N) | 相对本段前60s基线 (N) | Fx均值 (N) | Fy均值 (N) |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in bins:
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


def artifact_table() -> list[str]:
    rows = [
        "| Segment | raw CSV | summary | bins | full 图 | Fz 图 | fxy 图 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in SEGMENTS:
        d = RUN_ROOT / "segments" / name
        rows.append(
            "| "
            + " | ".join(
                [
                    name,
                    f"[raw_wrench.csv]({rel(d / 'raw_wrench.csv')})",
                    f"[summary.json]({rel(d / 'summary.json')})",
                    f"[drift_10min_bins.csv]({rel(d / 'drift_10min_bins.csv')})",
                    f"[force_torque.png]({rel(d / 'force_torque.png')})",
                    f"[fz.png]({rel(d / 'fz.png')})",
                    f"[fxy.png]({rel(d / 'fxy.png')})",
                ]
            )
            + " |"
        )
    return rows


def aborted_table(aborted: dict | None) -> list[str]:
    if not aborted:
        return ["无被取消片段。"]
    return [
        "| Segment | Planned duration (s) | Rows before abort | Last t_s (s) | Valid for report | Reason |",
        "|---|---:|---:|---:|---|---|",
        "| "
        + " | ".join(
            [
                aborted["segment"],
                str(aborted["planned_duration_s"]),
                str(aborted["rows_recorded_before_abort"]),
                f(aborted.get("last_row_before_abort", {}).get("t_s")),
                "False",
                aborted["aborted_reason"],
            ]
        )
        + " |",
        "",
        f"被取消片段保留原始 CSV 和中断记录：[{ABORTED_SEGMENT}/raw_wrench.csv](segments/{ABORTED_SEGMENT}/raw_wrench.csv)，[{ABORTED_SEGMENT}/ABORTED_TOUCHED.json](segments/{ABORTED_SEGMENT}/ABORTED_TOUCHED.json)。",
    ]


def segment_images() -> list[str]:
    rows: list[str] = []
    for name in SEGMENTS:
        rows.extend(
            [
                f"### {name}",
                "",
                f"![{name} full force/torque](segments/{name}/force_torque.png)",
                "",
                f"![{name} Fz](segments/{name}/fz.png)",
                "",
                f"![{name} Fx/Fy](segments/{name}/fxy.png)",
                "",
            ]
        )
    return rows


def main() -> int:
    summaries = segment_summaries()
    rests = rest_summaries()
    aborted = aborted_summary()
    manifest = load_json(RUN_ROOT / "protocol_manifest.json")
    events = protocol_events()

    lines: list[str] = [
        "# OnRobot HEX-E 自研末端安装后 60/600/1800 秒连续上电静置恢复测试",
        "",
        "## 实验目的",
        "",
        "本次实验记录 OnRobot HEX-E 安装在 UR10e 且已装上自研末端后的只读输出。UR10e 未开机，本实验不读 UR 状态、不发运动命令，只读取 OnRobot Compute Box 的 `/version` 与 Socket.IO force/torque stream。",
        "",
        "本次段间不是断电冷却，而是 `continuous-power rest/recovery`：Compute Box 持续上电。原计划第三段为 3600 s，但该段测量中用户触碰到装置，因此取消并改为新的 1800 s 有效段。报告中的 60 s、600 s、1800 s 只能解释为同一连续上电历史下的分段读数，不能解释为三个独立冷启动。",
        "",
        "## 设备与实验条件",
        "",
        "| 项目 | 内容 |",
        "|---|---|",
        "| 传感器 | OnRobot HEX-E v2，live serial 由各段 summary 记录 |",
        "| 自研末端 | 已安装在 HEX-E/UR10e 末端侧 |",
        "| UR10e | 未开机；不读 Dashboard/RTDE/ROS；不运动 |",
        "| Compute Box | `192.168.1.1`，预检 `/version` 为 `4.1.8` |",
        "| Ubuntu 网口 | `enp3s0`，`192.168.1.10/24` |",
        f"| 运行目录 | `{RUN_ROOT}` |",
        "| 段间状态 | Compute Box 持续上电；R00 完整静置 30 min；R01 因用户要求直接继续而提前结束 |",
        "| 清零/偏置 | 未调用 UR `zero_ftsensor()`；未调用 OnRobot zero/bias/autocalib |",
        "| 安全边界 | 无 firmware、DIP switch、配置写入、TCP/payload/gravity/ROS/UR 配置修改 |",
        "| 接触状态 | 没有设计接触动作；是否完全无环境接触以现场安装状态为准 |",
        "",
        "预检记录见 [preflight_notes.md](preflight_notes.md)，协议 manifest 见 [protocol_manifest.json](protocol_manifest.json)。",
        "",
        "## 实验命令",
        "",
        "三段有效测量均使用同一个只读 logger，`<SEGMENT>` 分别为 `S00_60s`、`S01_600s`、`S02_1800s`：",
        "",
        "```bash",
        "python3 tools/onrobot_socketio_logger.py \\",
        "  --host 192.168.1.1 \\",
        "  --duration-s <60|600|1800> \\",
        f"  --out-dir \"{RUN_ROOT}/segments/<SEGMENT>\" \\",
        "  --flush-every-s 10 \\",
        "  --status-every-s <30|120|300> \\",
        "  --baseline-s 60 \\",
        "  --bin-s 60",
        "```",
        "",
        "每段结束后统一重分析：",
        "",
        "```bash",
        "python3 tools/analyze_onrobot_drift.py \"$SEGMENT_DIR\" --baseline-s 60 --bin-s 60",
        "```",
        "",
        "## 数据与图片",
        "",
        *segment_images(),
        "## 统计结果",
        "",
        "Protocol table:",
        "",
        *protocol_table(summaries, rests),
        "",
        "被取消片段：",
        "",
        *aborted_table(aborted),
        "",
        "Fz 统一表：",
        "",
        *fz_table(summaries),
        "",
        "完整轴统计：",
        "",
        *axis_table(summaries),
        "",
        "## 60 秒 bin 趋势",
        "",
    ]
    for name in SEGMENTS:
        lines.extend(selected_bins(name))
        lines.append("")

    protocol_done = any(item.get("event") == "protocol_done" for item in events)
    failed = [item for item in events if item.get("event") == "protocol_failed"]
    fz0 = summaries["S00_60s"]["axis_stats"]["fzN"]
    fz2 = summaries["S02_1800s"]["axis_stats"]["fzN"]
    lines.extend(
        [
            "## 结论",
            "",
            f"本次协议完成状态：`protocol_done={protocol_done}`，`protocol_failed={len(failed)}`。三段有效采集均在 UR10e 关机、Compute Box 连续上电的条件下完成。`{ABORTED_SEGMENT}` 因触碰被取消，不进入有效统计比较。",
            "",
            f"从第一段首样本到第三段末样本，Fz 从 `{f(fz0['first'])} N` 到 `{f(fz2['last'])} N`。这个跨度包含两次 30 min 上电静置，不能当作单段漂移斜率。",
            "",
            "本次最重要的解释边界是：段间没有断电，因此结果反映 mounted EOAT 在 continuous-power 条件下的恢复/再稳定过程，不是严格温度冷却曲线。",
            "",
            "## 下一步",
            "",
            "若要得到真正冷却对比，需要加入可远程控制或定时的 Compute Box 24 V 电源开关；否则后续仍应把段间状态写作 continuous-power rest。若要做接触实验，需先完成低速无接触 cable sweep，并记录 TCP、payload、安装方向和现场接触状态。",
            "",
            "## Appendix A. 产物链接",
            "",
            *artifact_table(),
            "",
            "## Appendix B. Protocol Manifest",
            "",
            "```json",
            json.dumps(manifest, indent=2, ensure_ascii=False),
            "```",
            "",
        ]
    )

    (RUN_ROOT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(RUN_ROOT / "report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

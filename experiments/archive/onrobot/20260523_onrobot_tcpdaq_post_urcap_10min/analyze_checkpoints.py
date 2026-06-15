#!/usr/bin/env python3
"""Build checkpoint summaries for the 2026-05-23 OnRobot TCP DAQ run."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


RAW_KEYS = ["fx_raw", "fy_raw", "fz_raw", "tx_raw", "ty_raw", "tz_raw"]
VALUE_KEYS = ["fx_n", "fy_n", "fz_n", "tx_nm", "ty_nm", "tz_nm"]
FORCE_KEYS = ["fx_n", "fy_n", "fz_n"]
TORQUE_KEYS = ["tx_nm", "ty_nm", "tz_nm"]


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def fmean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def sample_std(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) >= 2 else None


def field_stats(rows: list[dict], key: str) -> dict:
    values = [float(row[key]) for row in rows]
    return {
        "first": values[0] if values else None,
        "last": values[-1] if values else None,
        "last_minus_first": (values[-1] - values[0]) if len(values) >= 2 else None,
        "mean": fmean(values),
        "std": sample_std(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "peak_to_peak": (max(values) - min(values)) if values else None,
        "p05": percentile(values, 0.05),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
    }


def timing_stats(rows: list[dict], source_row_count: int | None = None) -> dict:
    if not rows:
        return {
            "raw_tuple_samples_including_initial": 0,
            "raw_tuple_transitions": 0,
            "source_rows": source_row_count,
        }

    t_values = [float(row["t_s"]) for row in rows]
    duration = t_values[-1] - t_values[0] if len(t_values) >= 2 else 0.0
    dts = [b - a for a, b in zip(t_values, t_values[1:])]
    transitions = max(len(rows) - 1, 0)
    return {
        "raw_tuple_samples_including_initial": len(rows),
        "raw_tuple_transitions": transitions,
        "source_rows": source_row_count,
        "first_t_s": t_values[0],
        "last_t_s": t_values[-1],
        "duration_first_last_s": duration,
        "raw_tuple_transition_rate_hz": transitions / duration if duration else None,
        "mean_transition_dt_ms": fmean(dts) * 1000.0 if dts else None,
        "median_transition_dt_ms": statistics.median(dts) * 1000.0 if dts else None,
        "p95_transition_dt_ms": percentile(dts, 0.95) * 1000.0 if dts else None,
        "p99_transition_dt_ms": percentile(dts, 0.99) * 1000.0 if dts else None,
        "max_transition_dt_ms": max(dts) * 1000.0 if dts else None,
    }


def summarize(rows: list[dict], source_row_count: int | None = None) -> dict:
    summary = timing_stats(rows, source_row_count=source_row_count)
    summary["axis_stats"] = {key: field_stats(rows, key) for key in VALUE_KEYS}
    summary["force_norm_n"] = field_stats(rows, "force_norm_n")
    summary["status_values"] = sorted({int(row["status"]) for row in rows})
    return summary


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def plot_force_torque(rows: list[dict], output_path: Path, title: str) -> None:
    t0 = float(rows[0]["t_s"])
    ts = [float(row["t_s"]) - t0 for row in rows]
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)

    for key in FORCE_KEYS:
        axes[0].plot(ts, [float(row[key]) for row in rows], linewidth=0.8, label=key)
    axes[0].set_ylabel("Force (N)")
    axes[0].grid(True, alpha=0.25)
    axes[0].legend(loc="upper right")

    for key in TORQUE_KEYS:
        axes[1].plot(ts, [float(row[key]) for row in rows], linewidth=0.8, label=key)
    axes[1].set_ylabel("Torque (Nm)")
    axes[1].set_xlabel("Elapsed from first raw tuple change (s)")
    axes[1].grid(True, alpha=0.25)
    axes[1].legend(loc="upper right")

    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_fz(rows: list[dict], output_path: Path, title: str) -> None:
    t0 = float(rows[0]["t_s"])
    ts = [float(row["t_s"]) - t0 for row in rows]
    fz = [float(row["fz_n"]) for row in rows]
    mean_fz = statistics.fmean(fz)

    fig, ax = plt.subplots(figsize=(11, 4.5))
    ax.plot(ts, fz, linewidth=0.8, label="fz_n")
    ax.axhline(mean_fz, color="black", linewidth=0.8, linestyle="--", label="mean")
    ax.set_title(title)
    ax.set_xlabel("Elapsed from first raw tuple change (s)")
    ax.set_ylabel("Fz (N)")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def plot_fxy(rows: list[dict], output_path: Path, title: str) -> None:
    t0 = float(rows[0]["t_s"])
    ts = [float(row["t_s"]) - t0 for row in rows]
    fx = [float(row["fx_n"]) for row in rows]
    fy = [float(row["fy_n"]) for row in rows]

    fig, ax = plt.subplots(figsize=(11, 4.5))
    ax.plot(ts, fx, linewidth=0.8, label="fx_n")
    ax.plot(ts, fy, linewidth=0.8, label="fy_n")
    ax.set_title(title)
    ax.set_xlabel("Elapsed from first raw tuple change (s)")
    ax.set_ylabel("Force (N)")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def write_rows(path: Path, rows: list[dict]) -> None:
    fieldnames = [
        "sample_index",
        "t_s",
        "latency_ms",
        "status",
        *VALUE_KEYS,
        *RAW_KEYS,
        "force_norm_n",
    ]
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in fieldnames})


def load_raw_tuple_changes(csv_path: Path, output_csv: Path) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    prev_raw: tuple[int, ...] | None = None
    source_rows = 0
    source_first_t = None
    source_last_t = None
    latencies: list[float] = []

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open(newline="") as source, output_csv.open("w", newline="") as out:
        reader = csv.DictReader(source)
        fieldnames = [
            "sample_index",
            "t_s",
            "latency_ms",
            "status",
            *VALUE_KEYS,
            *RAW_KEYS,
            "force_norm_n",
        ]
        writer = csv.DictWriter(out, fieldnames=fieldnames)
        writer.writeheader()

        for row in reader:
            source_rows += 1
            t_s = float(row["t_s"])
            source_first_t = t_s if source_first_t is None else source_first_t
            source_last_t = t_s
            latencies.append(float(row["latency_ms"]))
            raw_tuple = tuple(int(row[key]) for key in RAW_KEYS)
            if prev_raw is not None and raw_tuple == prev_raw:
                continue
            converted = {
                "sample_index": int(row["sample_index"]),
                "t_s": t_s,
                "latency_ms": float(row["latency_ms"]),
                "status": int(row["status"]),
                **{key: float(row[key]) for key in VALUE_KEYS},
                **{key: int(row[key]) for key in RAW_KEYS},
            }
            converted["force_norm_n"] = math.sqrt(
                converted["fx_n"] ** 2 + converted["fy_n"] ** 2 + converted["fz_n"] ** 2
            )
            writer.writerow(converted)
            rows.append(converted)
            prev_raw = raw_tuple

    source_duration = (
        source_last_t - source_first_t
        if source_first_t is not None and source_last_t is not None
        else None
    )
    source_summary = {
        "source_rows": source_rows,
        "source_first_t_s": source_first_t,
        "source_last_t_s": source_last_t,
        "source_duration_first_last_s": source_duration,
        "source_row_rate_hz": source_rows / source_duration if source_duration else None,
        "mean_latency_ms": fmean(latencies),
        "p95_latency_ms": percentile(latencies, 0.95),
        "p99_latency_ms": percentile(latencies, 0.99),
        "max_latency_ms": max(latencies) if latencies else None,
        "raw_tuple_change_csv": str(output_csv),
    }
    return rows, source_summary


def make_checkpoint(
    rows: list[dict],
    source_summary: dict,
    seconds: int,
    output_root: Path,
) -> dict:
    start_t = float(rows[0]["t_s"])
    selected = [row for row in rows if float(row["t_s"]) - start_t <= seconds]
    checkpoint_dir = output_root / "checkpoints" / f"{seconds}s"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    write_rows(checkpoint_dir / "raw_tuple_changes.csv", selected)
    summary = summarize(selected)
    summary["checkpoint_seconds"] = seconds
    summary["source_csv_rows_total_run"] = source_summary["source_rows"]
    write_json(checkpoint_dir / "summary.json", summary)

    plot_force_torque(
        selected,
        checkpoint_dir / "force_torque.png",
        f"OnRobot TCP DAQ raw tuple changes, first {seconds}s",
    )
    plot_fz(selected, checkpoint_dir / "fz.png", f"Fz, first {seconds}s")
    plot_fxy(selected, checkpoint_dir / "fxy.png", f"Fx/Fy trace, first {seconds}s")

    return summary


def write_report(path: Path, source_csv: Path, source_summary: dict, checkpoints: dict) -> None:
    def fmt(value: float | int | None, digits: int = 4) -> str:
        if value is None:
            return "n/a"
        if isinstance(value, int):
            return str(value)
        return f"{value:.{digits}f}"

    dashboard_before = path.parent / "diagnostics" / "dashboard_before.json"
    dashboard_after = path.parent / "diagnostics" / "dashboard_after.json"
    dashboard_note = None
    if dashboard_before.exists() and dashboard_after.exists():
        before = json.loads(dashboard_before.read_text())
        after = json.loads(dashboard_after.read_text())
        before_state = before.get("responses", {}).get("programState")
        before_running = before.get("responses", {}).get("running")
        after_state = after.get("responses", {}).get("programState")
        after_running = after.get("responses", {}).get("running")
        dashboard_note = (
            f"测量前 Dashboard 为 `{before_running}`、`{before_state}`；"
            f"测量后为 `{after_running}`、`{after_state}`。"
        )

    lines = [
        "# OnRobot TCP DAQ 10 分钟只读测量报告",
        "",
        "## 实验目的",
        "",
        "本次实验验证当前 URCap/toolbar 状态下，Ubuntu 通过 OnRobot Compute Box `49151` TCP DAQ 只读读取时，raw tuple 新值率是否稳定接近 `250 Hz`。报告同时给出 30 s、120 s、600 s 三个累计检查点，用于判断短测结果是否能在 10 分钟内重复成立。",
        "",
        "## 设备与实验条件",
        "",
        "| 项目 | 内容 |",
        "|---|---|",
        "| 读取路径 | Ubuntu -> OnRobot Compute Box `192.168.1.1:49151` |",
        "| 命令边界 | 先发送一次 `READCALIBRATIONINFO`，随后只重复 `READFT` |",
        "| 安全边界 | 未发送 zero/bias/filter/speed/TCP/payload/URScript/program/motion 命令 |",
        "| 采样解释 | 源 CSV 行频率是 PC request/response 吞吐；raw tuple transition rate 才是该只读路径可观察的新值率 |",
        f"| 源 CSV | `{source_csv}` |",
        f"| 源 CSV 行数 | `{source_summary['source_rows']}` |",
        f"| 源 CSV first-last 时长 | `{fmt(source_summary['source_duration_first_last_s'])} s` |",
        f"| 源 CSV 行频率 | `{fmt(source_summary['source_row_rate_hz'])} Hz` |",
        f"| raw tuple transition CSV | `{source_summary['raw_tuple_change_csv']}` |",
        "",
        "## 程序状态与 250 Hz 解释",
        "",
    ]
    if dashboard_note:
        lines.append(dashboard_note)
    lines.extend(
        [
            "因此，这次 10 分钟测量证明：`wait 0.01` 程序没有正在运行时，仍然可以从 TCP DAQ 读到约 `251.5 Hz` 的 raw tuple transition rate。",
            "",
            "这不能反推出“之前短暂跑过 wait 程序就是唯一原因”。更稳妥的解释是：URCap/toolbar、Compute Box 或之前短暂运行程序可能把系统带入了一个会持续一段时间的 post-URCap/toolbar 状态；本次数据只证明该状态在程序停止后仍然存在。要验证是否由短暂程序运行激活，需要另做受控对比：重启或断电恢复后先测一次，再短暂运行/停止后再测一次。",
            "",
            "## 力值口径限制",
            "",
            "本报告的 `251.5 Hz` 结论只针对 TCP DAQ raw tuple 新值率，不证明 TCP DAQ 的力值零点已经和 PolyScope Variables 一致。2026-05-24 用户观察到 PolyScope `Variables` 里的 `Fz` 约为 `0.04 N`，而同一阶段 Ubuntu 只读 TCP DAQ 5 s 复核仍显示 `Fz mean = -32.7114 N`、raw tuple transition rate `249.9168 Hz`，Dashboard 仍为 `Program running: false`、`STOPPED <unnamed>`。",
            "",
            "因此，当前应把 PolyScope Variables 和 TCP DAQ `READFT` 视为两个不同口径：它们可能经过不同的 bias/zero、重力补偿、坐标/安装补偿或 URCap 变量处理。`wait 0.01` 程序是否会让 PolyScope Variables 正常刷新，和 TCP DAQ `READFT` 的 raw force 零点是否一致，是两个问题，不能直接混在一起判断。",
            "",
            "## 统计结果",
            "",
            "| 窗口 | raw tuple 数量（含首个） | transition 数 | transition 频率 | Fz 均值 | Fz std | Fz 末-首 | |F| 均值 | |F| 末-首 |",
        ]
    )
    lines.append(
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    )
    for seconds, summary in checkpoints.items():
        fz = summary["axis_stats"]["fz_n"]
        fn = summary["force_norm_n"]
        lines.append(
            "| "
            f"{seconds}s | "
            f"{summary['raw_tuple_samples_including_initial']} | "
            f"{summary['raw_tuple_transitions']} | "
            f"{fmt(summary['raw_tuple_transition_rate_hz'])} Hz | "
            f"{fmt(fz['mean'])} N | "
            f"{fmt(fz['std'])} N | "
            f"{fmt(fz['last_minus_first'])} N | "
            f"{fmt(fn['mean'])} N | "
            f"{fmt(fn['last_minus_first'])} N |"
        )

    lines.extend(
        [
            "",
            "## 数据与图片",
            "",
            "每个检查点都保存了 raw tuple transition CSV、统计 JSON、全量力/力矩图、Fz 单轴图、以及 fxy 图。这里的 fxy 按既定报告口径表示 Fx 和 Fy 随时间变化的两条曲线，不是 Fx-Fy 平面散点。",
            "",
        ]
    )
    for seconds in checkpoints:
        rel = f"checkpoints/{seconds}s"
        lines.extend(
            [
                f"### {seconds} s 检查点",
                "",
                f"![{seconds}s 全量力/力矩]({rel}/force_torque.png)",
                "",
                f"![{seconds}s Fz]({rel}/fz.png)",
                "",
                f"![{seconds}s Fx/Fy]({rel}/fxy.png)",
                "",
                f"- 数据：[{rel}/raw_tuple_changes.csv]({rel}/raw_tuple_changes.csv)",
                f"- 统计：[{rel}/summary.json]({rel}/summary.json)",
                "",
            ]
        )

    lines.extend(
        [
            "## 与旧 7 小时记录的关系",
            "",
            "旧 7 小时记录不是假数据，也不是这次频率问题的同一口径。旧记录用的是 `tools/onrobot_socketio_logger.py`，走 Web/Socket.IO 路径，约 `9 Hz`，而且当时 bench 状态不同。它适合解释那个路径和状态下的长时热漂/静态漂移，但不能直接拿来判断这次 `49151` TCP DAQ 的 raw tuple 新值率。",
            "",
            "## 结论",
            "",
            "1. 30 s、120 s、600 s 三个累计窗口的 raw tuple transition rate 都在约 `251.5 Hz`，说明短测看到的约 `250 Hz` 在本次 10 分钟内重复成立。",
            "2. 这个数字应写为“当前 post-URCap/toolbar 状态下，`49151` TCP DAQ raw tuple transition rate 约 `251.5 Hz`”。它不能写成全局 OnRobot 传感器采样频率，也不能替代 PolyScope URCap 变量更新率。",
            "3. 本次测量前后 Dashboard 都显示程序未运行，因此 `250 Hz` 不依赖 `wait 0.01` 程序正在运行。是否由之前短暂运行程序激活，只能作为假设，需要后续重启/断电前后对比验证。",
            "4. TCP DAQ 当前 Fz 约 `-32 N`，而 PolyScope Variables 可见 Fz 约 `0.04 N`；这说明目前不能把 TCP DAQ `READFT` 力值当作 PolyScope 变量力值的等价数据源。",
            "",
            "## 下一步",
            "",
            "如果要确认 `250 Hz` 是持久状态还是被某个 URCap/程序动作激活，下一步应做一次只读状态机实验：重启或断电恢复后先测 30 s；再只打开 toolbar/变量页测 30 s；再短暂运行并停止 no-motion 程序后测 30 s。每一步都记录 Dashboard `programState`、PolyScope Variables 的 `Fx/Fy/Fz/T*` 可见值、UR RTDE `actual_TCP_force`、以及 TCP DAQ raw tuple transition rate 和 Fz 均值。",
            "",
            "## 附录：复现命令",
            "",
            "采集命令：",
            "",
            "```bash",
            "python3 /home/andy/codex-private-skills/skills/ur10e-onrobot-hex/scripts/sample_onrobot_tcp_daq.py \\",
            "  --host 192.168.1.1 \\",
            "  --seconds 600 \\",
            "  --output-dir /home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260523_onrobot_tcpdaq_post_urcap_10min/onrobot_tcpdaq \\",
            "  --prefix onrobot_tcpdaq_post_urcap_10min",
            "```",
            "",
            "分析命令：",
            "",
            "```bash",
            "python3 /home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260523_onrobot_tcpdaq_post_urcap_10min/analyze_checkpoints.py \\",
            "  --csv /home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260523_onrobot_tcpdaq_post_urcap_10min/onrobot_tcpdaq/onrobot_tcpdaq_post_urcap_10min_20260523_234203.csv \\",
            "  --output-root /home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260523_onrobot_tcpdaq_post_urcap_10min",
            "```",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--checkpoints", nargs="+", type=int, default=[30, 120, 600])
    args = parser.parse_args()

    args.output_root.mkdir(parents=True, exist_ok=True)
    raw_change_csv = args.output_root / "analysis" / "raw_tuple_changes.csv"
    rows, source_summary = load_raw_tuple_changes(args.csv, raw_change_csv)
    if not rows:
        raise RuntimeError("no raw tuple changes found")

    checkpoints: dict[int, dict] = {}
    for seconds in args.checkpoints:
        checkpoints[seconds] = make_checkpoint(rows, source_summary, seconds, args.output_root)

    full_summary = {
        "source_csv": str(args.csv),
        "source_summary": source_summary,
        "full_raw_tuple_summary": summarize(rows, source_row_count=source_summary["source_rows"]),
        "checkpoints": checkpoints,
    }
    write_json(args.output_root / "analysis_summary.json", full_summary)
    write_report(args.output_root / "report.md", args.csv, source_summary, checkpoints)
    print(json.dumps(full_summary["full_raw_tuple_summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

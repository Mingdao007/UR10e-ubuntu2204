#!/usr/bin/env python3
from __future__ import annotations

import csv
import gzip
import hashlib
import html
import json
import math
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


REPO = Path("/home/andy/ur10e_ros2_ws")
EXP = REPO / "experiments/kunwei/closed-loop-straight-line/2026-06-04"
RUN = EXP / "runs/bridge_step4e_seed_normal_loop_v31_20260612_050155"
ASSETS = REPO / "report/assets/step4e-v31-surface-calibration"
REPORT = REPO / "report/step4e-v31-surface-calibration.md"
HTML = ASSETS / "calibration_review.html"
VIDEO_SOURCE_MAC = Path(
    "/Users/andyl/Documents/UR10e/archive/ur5e_legacy_20260518_223331/"
    "tase_reproduction_ur5e/TASE_Zhihao/reference_media/"
    "tase_supplementary_video_screenstudio_20260612.mp4"
)
VIDEO_LOCAL = EXP / "runs/video_reference_tase_supplementary_20260612/tase_supplementary_video_screenstudio_20260612.mp4"
REFERENCE_PREVIEW = REPO / "report/assets/step4fg-reference-preview"
SUPPLEMENTARY_SCREENSHOTS = REPO / "report/assets/tase-supplementary-video-screenshots"


RTDE_COLS = [
    "t_monotonic_s",
    "normal_force_n",
    "force_norm_n",
    "step4e_orientation_error_rad",
    "ur_output_double_register_35",
    "ur_output_double_register_31",
    "ur_actual_TCP_pose_0",
    "ur_actual_TCP_pose_1",
    "ur_actual_TCP_pose_2",
    "ur_actual_TCP_pose_3",
    "ur_actual_TCP_pose_4",
    "ur_actual_TCP_pose_5",
    "_step4e_control_normal_b_x",
    "_step4e_control_normal_b_y",
    "_step4e_control_normal_b_z",
    "_step4e_latched_normal_b_x",
    "_step4e_latched_normal_b_y",
    "_step4e_latched_normal_b_z",
    "_step4e_filtered_normal_b_x",
    "_step4e_filtered_normal_b_y",
    "_step4e_filtered_normal_b_z",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stats(values: Iterable[float]) -> dict[str, float | None]:
    arr = np.asarray(list(values), dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return {"mean": None, "median": None, "std": None, "min": None, "max": None, "p95_abs": None, "p99_abs": None}
    return {
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "std": float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "p95_abs": float(np.percentile(np.abs(arr), 95)),
        "p99_abs": float(np.percentile(np.abs(arr), 99)),
    }


def rotvec_to_matrix(rotvec: np.ndarray) -> np.ndarray:
    theta = float(np.linalg.norm(rotvec))
    if theta < 1e-12:
        return np.eye(3)
    k = rotvec / theta
    kx = np.array(
        [
            [0.0, -k[2], k[1]],
            [k[2], 0.0, -k[0]],
            [-k[1], k[0], 0.0],
        ]
    )
    return np.eye(3) + math.sin(theta) * kx + (1.0 - math.cos(theta)) * (kx @ kx)


def angle_deg(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    dot = np.sum(a * b, axis=1)
    return np.degrees(np.arccos(np.clip(dot, -1.0, 1.0)))


def unit_rows(values: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(values, axis=1)
    if np.any(norms < 1e-12):
        raise RuntimeError("zero-length normal vector in stage25 rows")
    return values / norms[:, None]


def run_cmd(args: list[str]) -> str:
    completed = subprocess.run(args, check=True, capture_output=True, text=True)
    return completed.stdout


def ffprobe_video(path: Path) -> dict[str, object]:
    data = json.loads(
        run_cmd(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,duration,avg_frame_rate",
                "-show_entries",
                "format=duration,size",
                "-of",
                "json",
                str(path),
            ]
        )
    )
    stream = data["streams"][0]
    fmt = data["format"]
    duration = float(fmt.get("duration") or stream.get("duration"))
    return {
        "duration_s": duration,
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "avg_frame_rate": stream.get("avg_frame_rate"),
        "size_bytes": int(fmt["size"]),
    }


def extract_video_frames(video: Path, info: dict[str, object]) -> list[dict[str, object]]:
    if not video.is_file():
        raise RuntimeError(f"video reference missing: {video}")
    frames_dir = ASSETS / "video-frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    duration = float(info["duration_s"])
    frame_times = [5.0, 20.0, 40.0, min(60.0, max(0.0, duration - 2.0))]
    frames = []
    for idx, time_s in enumerate(frame_times, start=1):
        out = frames_dir / f"tase-video-frame-{idx:02d}-{time_s:05.1f}s.jpg"
        run_cmd(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{time_s:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out),
            ]
        )
        frames.append({"time_s": time_s, "path": str(out.relative_to(REPO / "report"))})
    return frames


def load_latest_controller_manifest(program: str) -> dict[str, object] | None:
    matches = sorted((EXP / "runs").glob(f"controller_readback_{program}_*/manifest.json"))
    if not matches:
        return None
    data = json.loads(matches[-1].read_text())
    data["manifest_path"] = str(matches[-1])
    return data


def validate_readback_package(manifest: dict[str, object]) -> dict[str, object]:
    validation = manifest["validation"]
    program = validation["program"]
    target_dir = validation["target_dir"]
    readback_dir = Path(manifest["manifest_path"]).parent
    script_path = readback_dir / f"{program}.script"
    txt_path = readback_dir / f"{program}.txt"
    urp_path = readback_dir / f"{program}.urp"
    script = script_path.read_text(encoding="utf-8")
    txt = txt_path.read_text(encoding="utf-8")
    xml = gzip.decompress(urp_path.read_bytes()).decode("utf-8")
    root = ET.fromstring(xml)
    cached = ""
    script_node = ""
    for node in root.iter():
        if node.tag == "cachedContents":
            cached = html.unescape(node.text or "")
        elif node.tag == "file" and node.attrib.get("resolves-to") == "file":
            script_node = (node.text or "").strip()
    shape = "cycloid" if program.startswith("step4f_") else "eight"
    checks = {
        "status": manifest["status"] == "controller read-back verified",
        "local_controller_readback_sha_match": all(
            manifest["sha256"]["local"][ext] == manifest["sha256"]["controller"][ext] == manifest["sha256"]["readback"][ext]
            for ext in [".script", ".txt", ".urp"]
        ),
        "urp_name": root.attrib.get("name") == program,
        "urp_directory": root.attrib.get("directory") == target_dir,
        "script_node_path": script_node == f"{target_dir}/{program}.script",
        "cached_script_exact": cached == script,
        "stamp_in_script_txt_cache": validation["stamp"] in script and validation["stamp"] in txt and validation["stamp"] in cached,
        "path_shape": f"--step4e-path-shape {shape}" in script and shape in txt + script,
        "sixty_second_reference": "local line_runtime_limit_s = 65.000" in script
        and "local line_success_progress_m = 60.000000000" in script
        and "60 s" in txt + script,
    }
    return {
        "program": program,
        "controller_paths": {
            ext: f"{manifest['controller']}:{target_dir}/{program}{ext}" for ext in [".script", ".txt", ".urp"]
        },
        "readback_dir": str(readback_dir),
        "stamp": validation["stamp"],
        "checks": checks,
        "all_checks_ok": all(checks.values()),
        "sha256": manifest["sha256"],
    }


def synthetic_fixture() -> dict[str, object]:
    line_unit = np.array([0.6, 0.8])
    slope = 0.125
    s = np.linspace(0.0, 0.2, 500)
    z = 0.01 + slope * s
    center = float(np.mean(s))
    scale = float(np.ptp(s))
    x = (s - center) / scale
    coeff = np.polyfit(x, z, deg=1)
    dz_ds = np.polyval(np.polyder(coeff), x) / scale
    normal = unit_rows(np.column_stack([-dz_ds * line_unit[0], -dz_ds * line_unit[1], np.ones_like(dz_ds)]))
    expected = np.array([-slope * line_unit[0], -slope * line_unit[1], 1.0])
    expected = expected / np.linalg.norm(expected)
    normal_error_deg = angle_deg(normal, np.tile(expected, (len(normal), 1)))
    return {
        "known_slope_dz_ds": slope,
        "estimated_slope_mean": float(np.mean(dz_ds)),
        "max_slope_abs_error": float(np.max(np.abs(dz_ds - slope))),
        "max_normal_angle_error_deg": float(np.max(normal_error_deg)),
        "ok": float(np.max(np.abs(dz_ds - slope))) < 1e-9 and float(np.max(normal_error_deg)) < 1e-6,
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def rel(path: Path) -> str:
    return str(path.relative_to(REPO / "report"))


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"figure.dpi": 140, "savefig.dpi": 180, "font.size": 10})

    metadata = json.loads((RUN / "metadata.json").read_text())
    df = pd.read_csv(RUN / "bridge_rtde_500hz.csv", usecols=RTDE_COLS)
    df["stage"] = df["ur_output_double_register_35"]
    stage25 = df[df["stage"] == 25.0].copy()
    if stage25.empty:
        raise RuntimeError("stage25 line-control rows not found")
    stage25["stage25_t_s"] = stage25["t_monotonic_s"] - stage25["t_monotonic_s"].iloc[0]

    start_xy = np.array(metadata["step4e_path"]["start_xy_m"], dtype=float)
    line_unit = np.array(metadata["step4e_path"]["line_unit_xy"], dtype=float)
    xy = stage25[["ur_actual_TCP_pose_0", "ur_actual_TCP_pose_1"]].to_numpy(dtype=float)
    s = (xy - start_xy) @ line_unit
    z = stage25["ur_actual_TCP_pose_2"].to_numpy(dtype=float)
    center = float(np.mean(s))
    scale = float(np.ptp(s))
    if scale <= 0:
        raise RuntimeError("stage25 s coordinate is degenerate")
    x = (s - center) / scale
    degree = 3
    coeff = np.polyfit(x, z, degree)
    z_fit = np.polyval(coeff, x)
    dz_ds = np.polyval(np.polyder(coeff), x) / scale
    surface_normal = unit_rows(np.column_stack([-dz_ds * line_unit[0], -dz_ds * line_unit[1], np.ones_like(dz_ds)]))

    rotvec = stage25[["ur_actual_TCP_pose_3", "ur_actual_TCP_pose_4", "ur_actual_TCP_pose_5"]].to_numpy(dtype=float)
    tcp_z_axis = np.array([rotvec_to_matrix(rv)[:, 2] for rv in rotvec])
    control_normal = unit_rows(
        stage25[["_step4e_control_normal_b_x", "_step4e_control_normal_b_y", "_step4e_control_normal_b_z"]].to_numpy(dtype=float)
    )
    latched_normal = unit_rows(
        stage25[["_step4e_latched_normal_b_x", "_step4e_latched_normal_b_y", "_step4e_latched_normal_b_z"]].to_numpy(dtype=float)
    )
    filtered_normal = unit_rows(
        stage25[["_step4e_filtered_normal_b_x", "_step4e_filtered_normal_b_y", "_step4e_filtered_normal_b_z"]].to_numpy(dtype=float)
    )

    true_attitude_error_deg = angle_deg(tcp_z_axis, -surface_normal)
    bridge_proxy_error_deg = np.degrees(stage25["step4e_orientation_error_rad"].to_numpy(dtype=float))
    control_vs_surface_deg = angle_deg(control_normal, surface_normal)
    filtered_vs_surface_deg = angle_deg(filtered_normal, surface_normal)
    latched_vs_surface_deg = angle_deg(latched_normal, surface_normal)
    residual_mm = (z - z_fit) * 1000.0
    slope_deg = np.degrees(np.arctan(dz_ds))

    window_csv = ASSETS / "stage25-surface-calibration-window.csv"
    rows = []
    for i in range(0, len(stage25), max(1, len(stage25) // 2000)):
        rows.append(
            {
                "t_s": float(stage25["stage25_t_s"].iloc[i]),
                "s_mm": float(s[i] * 1000.0),
                "tcp_z_mm": float(z[i] * 1000.0),
                "z_fit_mm": float(z_fit[i] * 1000.0),
                "slope_deg": float(slope_deg[i]),
                "true_attitude_error_deg": float(true_attitude_error_deg[i]),
                "bridge_proxy_error_deg": float(bridge_proxy_error_deg[i]),
                "control_vs_surface_deg": float(control_vs_surface_deg[i]),
                "normal_force_n": float(stage25["normal_force_n"].iloc[i]),
            }
        )
    write_csv(window_csv, rows)

    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.scatter(s * 1000.0, z * 1000.0, s=2, alpha=0.25, label="stage25 TCP z samples")
    order = np.argsort(s)
    ax.plot(s[order] * 1000.0, z_fit[order] * 1000.0, color="#c43c2f", lw=2.0, label="cubic fitted surface profile")
    ax.set_xlabel("Line coordinate s (mm)")
    ax.set_ylabel("TCP z (mm)")
    ax.set_title("v31 stage25 surface profile from TCP Z")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    z_profile = ASSETS / "z-profile-fit.png"
    fig.savefig(z_profile)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 3.8))
    ax.plot(s[order] * 1000.0, slope_deg[order], color="#244a7f", lw=1.8)
    ax.axhline(0, color="#555", lw=0.8)
    ax.set_xlabel("Line coordinate s (mm)")
    ax.set_ylabel("Surface slope atan(dz/ds) (deg)")
    ax.set_title("Fitted local surface slope")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    slope_plot = ASSETS / "surface-slope.png"
    fig.savefig(slope_plot)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8.5, 4.3))
    ax.plot(stage25["stage25_t_s"], true_attitude_error_deg, label="TCP z vs -fitted surface normal", lw=1.4)
    ax.plot(stage25["stage25_t_s"], bridge_proxy_error_deg, label="bridge proxy orientation_error", lw=1.0, alpha=0.8)
    ax.plot(stage25["stage25_t_s"], control_vs_surface_deg, label="control normal vs fitted surface normal", lw=1.0, alpha=0.8)
    ax.set_xlabel("Stage25 time (s)")
    ax.set_ylabel("Angle (deg)")
    ax.set_title("True surface-derived attitude error")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    attitude_time = ASSETS / "attitude-error-time.png"
    fig.savefig(attitude_time)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8.5, 4.0))
    im = ax.scatter(true_attitude_error_deg, stage25["normal_force_n"], c=stage25["stage25_t_s"], s=4, cmap="viridis", alpha=0.65)
    ax.axhline(-5.0, color="#c43c2f", lw=1.0, label="signed target -5 N")
    ax.set_xlabel("True attitude error (deg)")
    ax.set_ylabel("Normal force Fz (N)")
    ax.set_title("Force response vs surface-derived attitude error")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.colorbar(im, ax=ax, label="Stage25 time (s)")
    fig.tight_layout()
    force_attitude = ASSETS / "force-vs-attitude-error.png"
    fig.savefig(force_attitude)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8.5, 4.0))
    ax.plot(s[order] * 1000.0, control_vs_surface_deg[order], label="control normal", lw=1.3)
    ax.plot(s[order] * 1000.0, filtered_vs_surface_deg[order], label="filtered normal", lw=1.0, alpha=0.85)
    ax.plot(s[order] * 1000.0, latched_vs_surface_deg[order], label="latched normal", lw=1.0, alpha=0.85)
    ax.set_xlabel("Line coordinate s (mm)")
    ax.set_ylabel("Angle to fitted surface normal (deg)")
    ax.set_title("Normal source comparison")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    normal_comparison = ASSETS / "normal-comparison.png"
    fig.savefig(normal_comparison)
    plt.close(fig)

    video_info = ffprobe_video(VIDEO_LOCAL)
    video_info["sha256"] = sha256(VIDEO_LOCAL)
    video_info["mac_source"] = str(VIDEO_SOURCE_MAC)
    video_info["local_ignored_copy"] = str(VIDEO_LOCAL)
    video_frames = extract_video_frames(VIDEO_LOCAL, video_info)
    video_info["frames"] = video_frames
    (ASSETS / "video-reference-manifest.json").write_text(json.dumps(video_info, indent=2) + "\n", encoding="utf-8")

    handoff = {
        program: validate_readback_package(load_latest_controller_manifest(program))
        for program in ["step4f_cycloid_seed_normal_v1", "step4g_eight_seed_normal_v1"]
    }
    (ASSETS / "controller-handoff-step4fg.json").write_text(json.dumps(handoff, indent=2) + "\n", encoding="utf-8")

    metrics = {
        "run_id": RUN.name,
        "analysis_window": {
            "stage": 25.0,
            "rows": int(len(stage25)),
            "duration_s": float(stage25["stage25_t_s"].iloc[-1] - stage25["stage25_t_s"].iloc[0]),
            "s_min_mm": float(np.min(s) * 1000.0),
            "s_max_mm": float(np.max(s) * 1000.0),
            "tcp_z_min_mm": float(np.min(z) * 1000.0),
            "tcp_z_max_mm": float(np.max(z) * 1000.0),
            "line_length_expected_mm": float(metadata["step4e_path"]["line_length_m"] * 1000.0),
        },
        "surface_fit": {
            "model": "z = cubic(s), fitted in normalized s",
            "degree": degree,
            "residual_mm": stats(residual_mm),
            "slope_deg": stats(slope_deg),
            "surface_normal_sign": "+z fitted normal; true attitude compares TCP z-axis to -surface_normal",
        },
        "attitude": {
            "true_tcp_z_vs_negative_surface_normal_deg": stats(true_attitude_error_deg),
            "bridge_proxy_orientation_error_deg": stats(bridge_proxy_error_deg),
            "control_normal_vs_surface_normal_deg": stats(control_vs_surface_deg),
            "filtered_normal_vs_surface_normal_deg": stats(filtered_vs_surface_deg),
            "latched_normal_vs_surface_normal_deg": stats(latched_vs_surface_deg),
        },
        "force": {
            "normal_force_n": stats(stage25["normal_force_n"]),
            "force_norm_n": stats(stage25["force_norm_n"]),
            "target_signed_normal_force_n": -5.0,
        },
        "sanity_checks": {
            "line_length_143_747_mm_ok": abs(float(metadata["step4e_path"]["line_length_m"] * 1000.0) - 143.747123) < 0.001,
            "tcp_z_range_matches_prior_report_ok": abs(float(np.min(z) * 1000.0) - 7.80) < 0.05
            and abs(float(np.max(z) * 1000.0) - 20.54) < 0.05,
            "surface_fit_residual_p95_lt_0_5mm": float(np.percentile(np.abs(residual_mm), 95)) < 0.5,
            "surface_normal_sign_resolved_by_z_up_and_control_normal": float(np.mean(np.sum(control_normal * surface_normal, axis=1))) > 0.98,
            "synthetic_plane_fixture": synthetic_fixture(),
        },
        "video_reference": video_info,
        "controller_handoff": handoff,
        "generated_assets": {
            "z_profile": rel(z_profile),
            "slope": rel(slope_plot),
            "attitude_time": rel(attitude_time),
            "force_attitude": rel(force_attitude),
            "normal_comparison": rel(normal_comparison),
            "stage25_surface_csv": rel(window_csv),
            "html_review": rel(HTML),
        },
    }
    (ASSETS / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")

    write_report(metrics)
    write_html(metrics)


def fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}"


def write_report(metrics: dict[str, object]) -> None:
    a = metrics["analysis_window"]
    fit = metrics["surface_fit"]
    attitude = metrics["attitude"]
    checks = metrics["sanity_checks"]
    video = metrics["video_reference"]
    report = f"""# Step4e v31 曲面姿态校正报告

## 实验目的

本报告把 v31 stage25 的 TCP XY/Z、rotvec、bridge normal 和 force log 转成一个 data-first 的局部曲面姿态检查。
目标不是重新证明 v31 已完成整条线，而是给下一轮姿态/normal 校正提供真实曲面派生的误差口径：先从 `z=f(s)` 拟合局部 surface profile，再比较 TCP z-axis 与 `-surface_normal` 的夹角。

## 设备与实验条件

| 字段 | 本次设置 |
|---|---:|
| 数据 run | `{metrics["run_id"]}` |
| 分析窗口 | stage `25.0` line-control |
| stage25 rows / duration | `{a["rows"]}` / `{fmt(a["duration_s"], 3)} s` |
| line coordinate range | `{fmt(a["s_min_mm"], 3)}..{fmt(a["s_max_mm"], 3)} mm` |
| expected line length | `{fmt(a["line_length_expected_mm"], 6)} mm` |
| TCP z range | `{fmt(a["tcp_z_min_mm"], 3)}..{fmt(a["tcp_z_max_mm"], 3)} mm` |
| surface fit | cubic `z=f(s)` in normalized line coordinate |
| normal sign | `+z` fitted surface normal；true attitude error 用 TCP z-axis 对 `-surface_normal` |
| UR zero / payload / TCP writes | 本分析不调用 `zero_ftsensor()`，不写 payload/TCP，不 load/play program |
| bridge/controller state | 使用已完成 v31 run log；本轮只做 controller file handoff 和 read-back validation |
| video reference | Mac source SHA `{video["sha256"][:16]}...`，duration `{fmt(video["duration_s"], 3)} s`，resolution `{video["width"]}x{video["height"]}` |

## 实验命令

报告由 [generate_surface_calibration.py](assets/step4e-v31-surface-calibration/generate_surface_calibration.py) 生成。
脚本读取 v31 `bridge_rtde_500hz.csv`、controller read-back manifest 和 ignored 本地视频副本，输出图表、metrics、抽帧和 review HTML。
完整命令放在附录。

## 数据与图片

图 1 是这次校正的主证据。
二次以上多项式已经足够解释 stage25 的 TCP z 变化；最终采用 cubic fit，p95 residual 为 `{fmt(fit["residual_mm"]["p95_abs"], 3)} mm`，max residual 为 `{fmt(fit["residual_mm"]["max"], 3)} mm`。

![v31 stage25 surface profile](assets/step4e-v31-surface-calibration/z-profile-fit.png)

图 2 展示由 `z=f(s)` 得到的局部坡度。
坡度不是常数，说明用单个 fixed normal 解释整段 line 会漏掉一部分真实曲面变化。

![surface slope](assets/step4e-v31-surface-calibration/surface-slope.png)

图 3 是真正的姿态误差口径。
蓝线是 TCP z-axis 与 `-surface_normal` 的夹角；它和 bridge proxy orientation error 不完全相同，因为 bridge proxy 仍来自控制 normal，而不是从 measured surface profile 反推的 surface normal。

![attitude error time](assets/step4e-v31-surface-calibration/attitude-error-time.png)

图 4 把 force 和 true attitude error 放在同一张散点图里。
这张图用于判断 force overshoot 是否和局部姿态误差共同出现；它不单独证明因果，只给后续 drag-teach 点校正和曲面姿态修正提供 evidence。

![force vs attitude error](assets/step4e-v31-surface-calibration/force-vs-attitude-error.png)

图 5 比较 control/filtered/latched normal 与 fitted surface normal。
control normal 和 fitted surface normal 的平均夹角为 `{fmt(attitude["control_normal_vs_surface_normal_deg"]["mean"], 3)} deg`，说明 surface-normal 符号可判定，但控制 normal 仍没有完全贴合曲面。

![normal comparison](assets/step4e-v31-surface-calibration/normal-comparison.png)

## 统计结果

### Surface fit 与 attitude

| 指标 | mean | median | p95 abs | min | max |
|---|---:|---:|---:|---:|---:|
| fit residual (mm) | `{fmt(fit["residual_mm"]["mean"])}` | `{fmt(fit["residual_mm"]["median"])}` | `{fmt(fit["residual_mm"]["p95_abs"])}` | `{fmt(fit["residual_mm"]["min"])}` | `{fmt(fit["residual_mm"]["max"])}` |
| slope angle (deg) | `{fmt(fit["slope_deg"]["mean"])}` | `{fmt(fit["slope_deg"]["median"])}` | `{fmt(fit["slope_deg"]["p95_abs"])}` | `{fmt(fit["slope_deg"]["min"])}` | `{fmt(fit["slope_deg"]["max"])}` |
| true attitude error (deg) | `{fmt(attitude["true_tcp_z_vs_negative_surface_normal_deg"]["mean"])}` | `{fmt(attitude["true_tcp_z_vs_negative_surface_normal_deg"]["median"])}` | `{fmt(attitude["true_tcp_z_vs_negative_surface_normal_deg"]["p95_abs"])}` | `{fmt(attitude["true_tcp_z_vs_negative_surface_normal_deg"]["min"])}` | `{fmt(attitude["true_tcp_z_vs_negative_surface_normal_deg"]["max"])}` |
| bridge proxy error (deg) | `{fmt(attitude["bridge_proxy_orientation_error_deg"]["mean"])}` | `{fmt(attitude["bridge_proxy_orientation_error_deg"]["median"])}` | `{fmt(attitude["bridge_proxy_orientation_error_deg"]["p95_abs"])}` | `{fmt(attitude["bridge_proxy_orientation_error_deg"]["min"])}` | `{fmt(attitude["bridge_proxy_orientation_error_deg"]["max"])}` |
| control normal vs fitted normal (deg) | `{fmt(attitude["control_normal_vs_surface_normal_deg"]["mean"])}` | `{fmt(attitude["control_normal_vs_surface_normal_deg"]["median"])}` | `{fmt(attitude["control_normal_vs_surface_normal_deg"]["p95_abs"])}` | `{fmt(attitude["control_normal_vs_surface_normal_deg"]["min"])}` | `{fmt(attitude["control_normal_vs_surface_normal_deg"]["max"])}` |

### Sanity checks

| 检查 | 结果 |
|---|---:|
| line length `143.747 mm` | `{checks["line_length_143_747_mm_ok"]}` |
| TCP Z range 与旧报告一致 | `{checks["tcp_z_range_matches_prior_report_ok"]}` |
| fit residual p95 `< 0.5 mm` | `{checks["surface_fit_residual_p95_lt_0_5mm"]}` |
| surface normal sign 可由 z-up/control-normal 判定 | `{checks["surface_normal_sign_resolved_by_z_up_and_control_normal"]}` |
| synthetic plane fixture | `{checks["synthetic_plane_fixture"]["ok"]}` |

### Step4f/4g controller handoff

| Program | Controller `.urp` | SHA/read-back/cache checks |
|---|---|---:|
| `step4f_cycloid_seed_normal_v1` | `/programs/andyl/kunwei/step4/step4f_cycloid_seed_normal_v1.urp` | `{metrics["controller_handoff"]["step4f_cycloid_seed_normal_v1"]["all_checks_ok"]}` |
| `step4g_eight_seed_normal_v1` | `/programs/andyl/kunwei/step4/step4g_eight_seed_normal_v1.urp` | `{metrics["controller_handoff"]["step4g_eight_seed_normal_v1"]["all_checks_ok"]}` |

## HTML review

交互 review 页在 [calibration_review.html](assets/step4e-v31-surface-calibration/calibration_review.html)。
它展示 v31 surface fit、Step4f/4g reference preview、supplementary screenshots 和本次视频抽帧。
这个页面现在只作为 evidence review，不再作为人工校正入口，也不要求导出 marker。

## Drag-teach 校正入口

正式校正入口改为 robot-side 示教点：

```bash
python3 experiments/kunwei/closed-loop-straight-line/2026-06-04/tools/capture_drag_teach_point.py start
python3 experiments/kunwei/closed-loop-straight-line/2026-06-04/tools/capture_drag_teach_point.py end
python3 experiments/kunwei/closed-loop-straight-line/2026-06-04/tools/analyze_drag_teach_points.py --shape both
```

`capture_drag_teach_point.py` 只读 Dashboard/RTDE，不发 URScript、不 load/start program、不 enable freedrive。
你在 teach pendant/robot 侧把 TCP 放到曲线起点和终点后分别 capture；analyzer 会输出公式点相对示教点的 `base X/Y` 偏差，以及 local `along/lateral` 偏差，单位都是 mm。
如果 start-only 平移后 end residual 超过阈值，报告会明确要求再 capture `mid`；否则不需要第三点。

## 结论

1. v31 stage25 的 `z=f(s)` 是可用的单值局部曲面 profile；cubic fit p95 residual `{fmt(fit["residual_mm"]["p95_abs"], 3)} mm`，没有触发“残差过大先讨论”的 gate。
2. surface normal 的符号可判定：`+z` fitted normal 与 control normal 同向，true attitude error 应比较 TCP z-axis 与 `-surface_normal`。
3. true surface-derived attitude error 平均 `{fmt(attitude["true_tcp_z_vs_negative_surface_normal_deg"]["mean"], 3)} deg`，p95 `{fmt(attitude["true_tcp_z_vs_negative_surface_normal_deg"]["p95_abs"], 3)} deg`；它和 bridge proxy error 有系统差异，后续校正不应只看 proxy。
4. Step4f/4g 现在可以称为 TP-ready：controller 上的 `.script/.txt/.urp` triplets 已上传并 read-back 校验，且 `.urp cachedContents` 与 `.script` 精确一致。

## 下一步

- 先由你物理示教 `start`/`end`，必要时补 `mid`。
- 根据 analyzer 给出的 `base X/Y` 偏差决定 TP anchor 是否只做平移；如果 end/mid residual 说明存在旋转或曲率误差，先讨论 anchor，不直接改 TP program。
- 下一轮控制侧只改 surface/normal 校正口径，保持 v31/Step4f/Step4g TP scaffold 不变。

## 附录

### 生成命令

```bash
python3 /home/andy/ur10e_ros2_ws/report/assets/step4e-v31-surface-calibration/generate_surface_calibration.py
```

### 关键 artifacts

| artifact | 路径 |
|---|---|
| metrics | [metrics.json](assets/step4e-v31-surface-calibration/metrics.json) |
| controller handoff | [controller-handoff-step4fg.json](assets/step4e-v31-surface-calibration/controller-handoff-step4fg.json) |
| video manifest | [video-reference-manifest.json](assets/step4e-v31-surface-calibration/video-reference-manifest.json) |
| stage25 downsampled calibration CSV | [stage25-surface-calibration-window.csv](assets/step4e-v31-surface-calibration/stage25-surface-calibration-window.csv) |
"""
    REPORT.write_text(report, encoding="utf-8")


def image_card(path: str, title: str) -> str:
    return f'<figure class="card" data-asset="{html.escape(path)}"><img src="../{html.escape(path)}" alt="{html.escape(title)}"><figcaption>{html.escape(title)}</figcaption></figure>'


def write_html(metrics: dict[str, object]) -> None:
    frames = metrics["video_reference"]["frames"]
    images = [
        ("step4e-v31-surface-calibration/z-profile-fit.png", "v31 surface profile"),
        ("step4e-v31-surface-calibration/surface-slope.png", "surface slope"),
        ("step4e-v31-surface-calibration/attitude-error-time.png", "true attitude error"),
        ("step4e-v31-surface-calibration/force-vs-attitude-error.png", "force vs attitude error"),
        ("step4e-v31-surface-calibration/normal-comparison.png", "normal comparison"),
        ("step4fg-reference-preview/step4f_cycloid_reference_preview.png", "Step4f cycloid reference preview"),
        ("step4fg-reference-preview/step4g_eight_reference_preview.png", "Step4g eight reference preview"),
        ("tase-supplementary-video-screenshots/experiment-1-cycloid-small-surface.jpeg", "supplementary screenshot: cycloid small surface"),
        ("tase-supplementary-video-screenshots/experiment-2-eight-small-surface.jpeg", "supplementary screenshot: eight small surface"),
    ]
    images.extend((str(Path(frame["path"]).relative_to("assets")), f"TASE video frame @ {frame['time_s']:.1f}s") for frame in frames)
    cards = "\n".join(image_card(path, title) for path, title in images)
    metrics_blob = html.escape(json.dumps(metrics, ensure_ascii=False, indent=2))
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Step4e v31 Surface Calibration Review</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #1d242b;
      --muted: #5e6a73;
      --line: #d5dde5;
      --accent: #0f766e;
      --bg: #f7f9fb;
      --panel: #ffffff;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ max-width: 100%; overflow-x: hidden; }}
    body {{ margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: var(--ink); background: var(--bg); }}
    header {{ padding: 24px 28px 16px; background: var(--panel); border-bottom: 1px solid var(--line); }}
    h1 {{ margin: 0 0 8px; font-size: 26px; line-height: 1.2; }}
    h2 {{ margin: 24px 0 12px; font-size: 18px; }}
    p {{ max-width: 980px; line-height: 1.58; color: var(--muted); }}
    main {{ display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 18px; padding: 18px 24px 28px; min-width: 0; }}
    section, .grid, .card, aside, pre {{ min-width: 0; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; align-items: start; }}
    .card {{ margin: 0; padding: 10px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; position: relative; overflow: hidden; }}
    .card img {{ display: block; width: 100%; max-width: 100%; height: auto; border-radius: 4px; }}
    figcaption {{ margin-top: 8px; font-size: 13px; color: var(--muted); }}
    aside {{ position: sticky; top: 14px; align-self: start; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }}
    code {{ background: #eef3f6; border: 1px solid var(--line); border-radius: 4px; padding: 1px 4px; }}
    .step-list {{ margin: 8px 0 0; padding-left: 18px; color: var(--muted); line-height: 1.55; }}
    pre {{ white-space: pre-wrap; overflow: auto; background: #111820; color: #d9e5ee; padding: 12px; border-radius: 8px; font-size: 12px; }}
    @media (max-width: 900px) {{
      main {{ grid-template-columns: 1fr; padding: 14px; }}
      aside {{ position: static; }}
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Step4e v31 Surface Calibration Review</h1>
    <p>只读 evidence review：这里展示 v31 曲面拟合、Step4f/4g reference preview、supplementary screenshots 和视频抽帧。校正入口已改为 robot-side drag-teach start/end 点。</p>
  </header>
  <main>
    <section>
      <h2>Review Assets</h2>
      <div class="grid">{cards}</div>
      <h2>Metrics Snapshot</h2>
      <pre>{metrics_blob}</pre>
    </section>
    <aside>
      <h2>Calibration Entry</h2>
      <p>不要在这个 HTML 上点 marker。正式入口是在 robot/teach pendant 侧把 TCP 放到曲线点位后，用只读 RTDE snapshot capture。</p>
      <ol class="step-list">
        <li><code>capture_drag_teach_point.py start</code></li>
        <li><code>capture_drag_teach_point.py end</code></li>
        <li><code>analyze_drag_teach_points.py --shape both</code></li>
      </ol>
      <p>Analyzer 会输出公式 reference 相对示教点的 base X/Y 偏差；只有 end residual 超阈值时才需要第三个 <code>mid</code> 点。</p>
    </aside>
  </main>
</body>
</html>
"""
    HTML.write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()

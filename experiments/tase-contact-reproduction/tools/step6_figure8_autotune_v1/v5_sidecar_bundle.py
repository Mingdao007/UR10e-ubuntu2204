"""Canonical Autotuner V5 sidecar bundle configuration contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

try:  # Tests may put ``tools`` directly on sys.path.
    from step6_figure8_autotune_v1.v5_filter_shadow import (
        FilterShadowJobQueueV1,
        FilterShadowJobV1,
    )
except ModuleNotFoundError:  # pragma: no cover - repository-root imports
    from tools.step6_figure8_autotune_v1.v5_filter_shadow import (
        FilterShadowJobQueueV1,
        FilterShadowJobV1,
    )


SIDECAR_BUNDLE_SCHEMA = "step6.autotune/autotuner-v5-sidecars-v1"
SIDECAR_BUNDLE_VERSION = 1
SIDECAR_SUBMISSION_SCHEMA = "step6.autotune/autotuner-v5-sidecar-submissions-v1"
SIDECAR_SUBMISSION_VERSION = 1
GENESIS_SHA256 = "0" * 64


class V5SidecarBundleError(RuntimeError):
    """The V5 observation-only sidecar bundle contract differs."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _valid_sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_chain_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and all(
            character
            in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in value
        )
    )


@dataclass(frozen=True)
class V5SidecarBundleConfigV1:
    raw: Mapping[str, Any]
    schema: str = SIDECAR_BUNDLE_SCHEMA
    version: int = SIDECAR_BUNDLE_VERSION

    def __post_init__(self) -> None:
        if self.schema != SIDECAR_BUNDLE_SCHEMA or self.version != SIDECAR_BUNDLE_VERSION or not isinstance(self.raw, Mapping):
            raise V5SidecarBundleError("V5 sidecar config schema/version differs")
        raw = dict(self.raw)
        expected_top = {"schema", "version", "status", "auto_start", "physical_campaign_dependency", "control_authority", "single_live_writer_unchanged", "failure_policy", "filter_shadow", "camera_observer", "ros2_observation_mirror"}
        if set(raw) != expected_top or raw.get("status") != "implemented_explicit_launch_only" or raw.get("auto_start") is not False or raw.get("physical_campaign_dependency") is not False or raw.get("control_authority") is not False or raw.get("single_live_writer_unchanged") is not True or raw.get("failure_policy") != "isolate_and_record":
            raise V5SidecarBundleError("V5 sidecar top-level authority differs")
        filter_shadow = raw.get("filter_shadow")
        if not isinstance(filter_shadow, Mapping) or filter_shadow != {
            "input": "sealed_r013life_artifact_only",
            "run_phase": "post_home",
            "queue_capacity": 2,
            "overflow_policy": "drop_newest",
            "worker_count": 1,
            "anchor_tau_s": 0.04375,
            "candidates": ["actual_dt_one_pole_lpf", "same_cutoff_two_pole_bessel", "same_cutoff_two_pole_butterworth"],
            "notch_50_hz_enabled": False,
            "authority": "observation_only",
        }:
            raise V5SidecarBundleError("V5 filter-shadow contract differs")
        camera = raw.get("camera_observer")
        if not isinstance(camera, Mapping) or camera != {
            "retention_policy_version": 2,
            "hard_quota_bytes": 1073741824,
            "soft_target_jpeg_frames": 1000,
            "metadata_reserve_bytes": 1048576,
            "active_event_pin_ttl_s": 900.0,
            "active_event_pin_max_bytes": 268435456,
            "quota_drop_policy": "drop_new_frame_and_record_quota_drop_when_only_pinned_frames_remain",
            "input": "rtsp://127.0.0.1:8554/arm",
            "single_reader": True,
            "ring_duration_s": 15.0,
            "regular_sample_hz": 1.0,
            "event_capture": True,
            "ring_overflow_policy": "drop_oldest",
            "stale_after_s": 2.5,
            "stale_status": "UNKNOWN",
            "authority": "advisory_only",
            "controller_write_allowed": False,
            "safety_bypass_allowed": False,
        }:
            raise V5SidecarBundleError("V5 camera observer contract differs")
        ros2 = raw.get("ros2_observation_mirror")
        if not isinstance(ros2, Mapping) or ros2 != {
            "sample_rate_hz": 10.0,
            "events_bypass_rate_limit": True,
            "queue_capacity": 128,
            "overflow_policy": "drop_newest",
            "topics": ["/autotuner_v5/observations", "/autotuner_v5/events"],
            "message_type": "std_msgs/msg/String",
            "storage_id": "sqlite3",
            "rosbag2": True,
            "authority": "observation_only",
            "subscribers_allowed": False,
            "command_topics_allowed": False,
            "second_rtde_or_kunwei_reader_allowed": False,
        }:
            raise V5SidecarBundleError("V5 ROS2 observation contract differs")
        object.__setattr__(self, "raw", raw)

    @classmethod
    def from_path(cls, path: Path | str) -> "V5SidecarBundleConfigV1":
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise V5SidecarBundleError("V5 sidecar config is not a regular file")
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise V5SidecarBundleError("V5 sidecar config cannot be read") from exc
        if not isinstance(value, Mapping):
            raise V5SidecarBundleError("V5 sidecar config is not an object")
        return cls(value, schema=value.get("schema"), version=value.get("version"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical(dict(self.raw))).hexdigest()

    def receipt(self) -> dict[str, Any]:
        return {
            "schema": "step6.autotune/autotuner-v5-sidecars-contract-receipt-v1",
            "version": 1,
            "config_sha256": self.sha256,
            "status": self.raw["status"],
            "auto_start": False,
            "physical_campaign_dependency": False,
            "control_authority": False,
            "sidecars": ["filter_shadow", "camera_observer", "ros2_observation_mirror"],
        }


class V5PostHomeSidecarFanoutV1:
    """Durable, non-authoritative post-Home FilterShadow submission fan-out.

    The physical owner has already sealed the lifecycle artifact and returned
    Home before this object is called.  Submission failures are converted to
    observation-only receipts and never escape into campaign accounting.
    """

    def __init__(
        self,
        root: Path | str,
        config: V5SidecarBundleConfigV1,
    ) -> None:
        if not isinstance(config, V5SidecarBundleConfigV1):
            raise TypeError("post-Home sidecar fan-out requires a typed config")
        self.root = Path(root).resolve()
        if self.root.is_symlink():
            raise V5SidecarBundleError("sidecar state root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.queue = FilterShadowJobQueueV1(self.root / "filter-shadow-queue")
        self.output_root = self.root / "filter-shadow-output"
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "filter-shadow-submissions.jsonl"
        if self.index_path.is_symlink():
            raise V5SidecarBundleError("sidecar submission index must not be a symlink")
        if not self.index_path.exists():
            with self.index_path.open("x", encoding="utf-8") as stream:
                stream.write(
                    _canonical(
                        {
                            "schema": SIDECAR_SUBMISSION_SCHEMA,
                            "version": SIDECAR_SUBMISSION_VERSION,
                            "record_type": "header",
                            "config_sha256": self.config.sha256,
                            "genesis_sha256": GENESIS_SHA256,
                            "physical_campaign_dependency": False,
                            "control_authority": False,
                        }
                    ).decode("utf-8")
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
        self._head_sha256 = GENESIS_SHA256
        self._receipts: dict[str, dict[str, Any]] = {}
        self.cold_verify()

    @property
    def receipts(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(dict(value) for value in self._receipts.values())

    def cold_verify(self) -> str:
        try:
            rows = [
                json.loads(line)
                for line in self.index_path.read_text(encoding="utf-8").splitlines()
            ]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise V5SidecarBundleError("sidecar submission index is unreadable") from exc
        header = {
            "schema": SIDECAR_SUBMISSION_SCHEMA,
            "version": SIDECAR_SUBMISSION_VERSION,
            "record_type": "header",
            "config_sha256": self.config.sha256,
            "genesis_sha256": GENESIS_SHA256,
            "physical_campaign_dependency": False,
            "control_authority": False,
        }
        if not rows or rows[0] != header:
            raise V5SidecarBundleError("sidecar submission index header differs")
        previous = GENESIS_SHA256
        receipts: dict[str, dict[str, Any]] = {}
        required = {
            "schema",
            "version",
            "record_type",
            "config_sha256",
            "previous_sha256",
            "chain_id",
            "source_artifact_sha256",
            "job_id",
            "accepted",
            "disposition",
            "error_type",
            "error",
            "physical_campaign_dependency",
            "control_authority",
            "row_sha256",
        }
        for row in rows[1:]:
            if not isinstance(row, dict):
                raise V5SidecarBundleError("sidecar submission index row is not an object")
            unsigned = {key: value for key, value in row.items() if key != "row_sha256"}
            chain_id = row.get("chain_id")
            artifact_sha = row.get("source_artifact_sha256")
            if (
                set(row) != required
                or row.get("schema") != SIDECAR_SUBMISSION_SCHEMA
                or row.get("version") != SIDECAR_SUBMISSION_VERSION
                or row.get("record_type") != "filter_shadow_submission"
                or row.get("config_sha256") != self.config.sha256
                or row.get("previous_sha256") != previous
                or row.get("physical_campaign_dependency") is not False
                or row.get("control_authority") is not False
                or row.get("row_sha256") != _sha(unsigned)
                or not _valid_chain_id(chain_id)
                or not _valid_sha(artifact_sha)
                or chain_id in receipts
            ):
                raise V5SidecarBundleError("sidecar submission index row differs")
            if row.get("accepted") is True:
                if (
                    not _valid_sha(row.get("job_id"))
                    or row.get("disposition") not in {"ENQUEUED", "DUPLICATE"}
                    or row.get("error_type") is not None
                    or row.get("error") is not None
                ):
                    raise V5SidecarBundleError("accepted sidecar submission receipt differs")
            elif row.get("accepted") is False:
                if row.get("job_id") is not None and not _valid_sha(row.get("job_id")):
                    raise V5SidecarBundleError("rejected sidecar job identity differs")
                if row.get("disposition") == "DROP_NEWEST":
                    if row.get("error_type") is not None or row.get("error") is not None:
                        raise V5SidecarBundleError("overload sidecar receipt carries an error")
                elif row.get("disposition") == "FAILED_ISOLATED":
                    if not isinstance(row.get("error_type"), str) or not isinstance(row.get("error"), str):
                        raise V5SidecarBundleError("isolated sidecar failure lacks its error")
                else:
                    raise V5SidecarBundleError("rejected sidecar disposition differs")
            else:
                raise V5SidecarBundleError("sidecar submission acceptance is not boolean")
            receipts[chain_id] = dict(row)
            previous = str(row["row_sha256"])
        self._receipts = receipts
        self._head_sha256 = previous
        return previous

    def _append(self, body: Mapping[str, Any]) -> dict[str, Any]:
        row = {
            "schema": SIDECAR_SUBMISSION_SCHEMA,
            "version": SIDECAR_SUBMISSION_VERSION,
            "record_type": "filter_shadow_submission",
            "config_sha256": self.config.sha256,
            "previous_sha256": self._head_sha256,
            **dict(body),
            "physical_campaign_dependency": False,
            "control_authority": False,
        }
        row["row_sha256"] = _sha(row)
        with self.index_path.open("a", encoding="utf-8") as stream:
            stream.write(_canonical(row).decode("utf-8") + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.cold_verify()
        return dict(self._receipts[str(body["chain_id"])])

    def try_submit_filter_shadow(
        self,
        *,
        chain_id: str,
        artifact_path: Path | str,
        lifecycle_receipt: Mapping[str, Any],
        event_bundle: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Submit one immutable artifact job; all sidecar failures stay local."""

        try:
            if not _valid_chain_id(chain_id):
                raise V5SidecarBundleError("filter-shadow chain identity is invalid")
            self.cold_verify()
            if chain_id in self._receipts:
                return dict(self._receipts[chain_id])
            artifact_sha = lifecycle_receipt.get("artifact_sha256")
            if not _valid_sha(artifact_sha):
                raise V5SidecarBundleError("filter-shadow source artifact hash is invalid")
            output_path = self.output_root / f"{artifact_sha}.json"
            job = FilterShadowJobV1(
                str(Path(artifact_path).resolve()),
                dict(lifecycle_receipt),
                dict(event_bundle),
                str(output_path.resolve()),
                tau_s=float(self.config.raw["filter_shadow"]["anchor_tau_s"]),
            )
            submission = self.queue.submit(job)
            return self._append(
                {
                    "chain_id": chain_id,
                    "source_artifact_sha256": artifact_sha,
                    "job_id": job.job_id,
                    "accepted": bool(submission["accepted"]),
                    "disposition": str(submission["disposition"]),
                    "error_type": None,
                    "error": None,
                }
            )
        except Exception as exc:  # noqa: BLE001 -- mandatory isolation boundary
            artifact_sha = lifecycle_receipt.get("artifact_sha256")
            body = {
                "chain_id": str(chain_id),
                "source_artifact_sha256": (
                    artifact_sha if _valid_sha(artifact_sha) else GENESIS_SHA256
                ),
                "job_id": None,
                "accepted": False,
                "disposition": "FAILED_ISOLATED",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            try:
                if not _valid_chain_id(chain_id):
                    raise V5SidecarBundleError("failed sidecar submission cannot be indexed without a valid chain identity")
                if str(chain_id) in self._receipts:
                    return dict(self._receipts[str(chain_id)])
                return self._append(body)
            except Exception as index_exc:  # noqa: BLE001 -- must not reach physical campaign
                return {
                    "schema": SIDECAR_SUBMISSION_SCHEMA,
                    "version": SIDECAR_SUBMISSION_VERSION,
                    "record_type": "filter_shadow_submission",
                    "config_sha256": self.config.sha256,
                    "previous_sha256": self._head_sha256,
                    **body,
                    "physical_campaign_dependency": False,
                    "control_authority": False,
                    "row_sha256": None,
                    "index_durable": False,
                    "index_error_type": type(index_exc).__name__,
                    "index_error": str(index_exc),
                }

    def snapshot(self) -> dict[str, Any]:
        """Return a bounded cold status; it never drains or joins a worker."""

        self.cold_verify()
        counts: dict[str, int] = {}
        for receipt in self._receipts.values():
            disposition = str(receipt["disposition"])
            counts[disposition] = counts.get(disposition, 0) + 1
        queue_counts = {
            name: len(tuple((self.queue.root / name).glob("*.json")))
            for name in ("pending", "inflight", "completed", "failed")
        }
        return {
            "schema": "step6.autotune/autotuner-v5-sidecar-status-v1",
            "version": 1,
            "contract": self.config.receipt(),
            "filter_shadow": {
                "submission_count": len(self._receipts),
                "disposition_counts": counts,
                "queue_counts": queue_counts,
                "submission_index": str(self.index_path),
                "submission_index_head_sha256": self._head_sha256,
                "worker_launch": "explicit_only",
            },
            "camera_observer": {"launch": "explicit_only", "physical_campaign_dependency": False},
            "ros2_observation_mirror": {"launch": "explicit_only", "physical_campaign_dependency": False},
            "physical_campaign_dependency": False,
            "control_authority": False,
        }


__all__ = [
    "SIDECAR_BUNDLE_SCHEMA",
    "SIDECAR_SUBMISSION_SCHEMA",
    "V5PostHomeSidecarFanoutV1",
    "V5SidecarBundleConfigV1",
    "V5SidecarBundleError",
]

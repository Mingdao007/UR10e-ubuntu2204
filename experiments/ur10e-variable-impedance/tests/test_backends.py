from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import tempfile
import unittest

from ur10e_vic.backends import (
    CapabilitySeparatedVelocityMux,
    DirectTorqueGuardState,
    DirectTorquePacket,
    Step5bTwistInput,
    VelocityAdmittanceSurrogate,
    direct_torque_supported,
    load_direct_torque_bundle,
    load_step5b_scaffold_binding,
    require_direct_torque_version,
    torque_formula_reference,
    validate_direct_torque_packet,
)
from ur10e_vic.constraints import ImpedanceBounds
from ur10e_vic.policies import FixedImpedancePolicy

from helpers import observation


ROOT = Path(__file__).resolve().parents[1]


class BackendTests(unittest.TestCase):
    @staticmethod
    def _torque_packet(
        sequence: int,
        *,
        x: float = 0.4,
        stiffness=(600.0, 600.0, 600.0, 30.0, 30.0, 30.0),
        sequence_before: int | None = None,
        heartbeat: int | None = None,
        lease: int = 77,
    ) -> DirectTorquePacket:
        mass = (2.0, 2.0, 2.0, 0.2, 0.2, 0.2)
        damping = tuple(
            2.0 * math.sqrt(stiffness[index] * mass[index])
            for index in range(6)
        )
        return DirectTorquePacket(
            sequence_before=sequence if sequence_before is None else sequence_before,
            sequence_after=sequence,
            heartbeat=sequence if heartbeat is None else heartbeat,
            lease_id=lease,
            mode=1,
            equilibrium_pose=(x, 0.1, 0.05, 3.14, 0.0, 0.0),
            stiffness=stiffness,
            damping=damping,
        )

    def test_direct_torque_version_gate(self) -> None:
        self.assertFalse(direct_torque_supported("URSoftware 5.11.9.1010452"))
        self.assertTrue(direct_torque_supported("PolyScope 5.23"))
        with self.assertRaisesRegex(RuntimeError, "requires PolyScope 5.23"):
            require_direct_torque_version("5.11.9")

    def test_layout_is_hash_bound_and_defaults_disabled(self) -> None:
        payload = load_direct_torque_bundle(
            ROOT / "config/direct_torque_rtde_layout.json",
            ROOT / "programs/direct_torque_vic_offline_template.script",
        )
        self.assertFalse(payload["upload_authorized"])
        self.assertEqual(payload["default_mode"], "disabled")
        self.assertIn("get_jacobian", payload["official_api_evidence"])

    def test_layout_limit_value_drift_is_rejected(self) -> None:
        layout = json.loads(
            (ROOT / "config/direct_torque_rtde_layout.json").read_text(
                encoding="utf-8"
            )
        )
        layout["limits"]["force_norm_abs_max_n"] = 500.0
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "layout.json"
            candidate.write_text(json.dumps(layout), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "limits drifted"):
                load_direct_torque_bundle(
                    candidate,
                    ROOT / "programs/direct_torque_vic_offline_template.script",
                )

    def test_urscript_is_uninvoked_523_template_with_fail_closed_tokens(self) -> None:
        script = (ROOT / "programs/direct_torque_vic_offline_template.script").read_text()
        required = (
            "OFFLINE TEMPLATE ONLY",
            "get_jacobian(q)",
            "get_coriolis_and_centrifugal_torques(q, qd)",
            "direct_torque(tau)",
            "sequence_before == sequence_after and heartbeat == sequence",
            "sequence == last_sequence + 1",
            "vic_stiffness_transition_valid(stiffness, last_stiffness, get_steptime())",
            "vic_damping_matches_sqrt(stiffness, damping)",
            "vic_equilibrium_transition_valid",
            "vic_release_ready(eq)",
            "vic_runtime_guard_ok()",
            "zero_startup_count < zero_startup_ticks",
            "packet_lease > 0",
            "stopj(10.0)",
            "Intentionally no invocation",
        )
        for token in required:
            self.assertIn(token, script)
        self.assertNotIn("socket_open", script)
        self.assertNotIn("run program", script.lower())
        invocation = "ur10e_vic_direct_torque_offline_template()"
        self.assertFalse(
            any(
                line.strip() == invocation
                for line in script.splitlines()
                if not line.lstrip().startswith("#")
            )
        )
        payload = load_direct_torque_bundle(
            ROOT / "config/direct_torque_rtde_layout.json",
            ROOT / "programs/direct_torque_vic_offline_template.script",
        )
        self.assertTrue(payload["torque_backend_exclusive"])
        self.assertTrue(payload["translational_vic_only"])

    def test_511_surrogate_is_bounded_and_cannot_claim_torque(self) -> None:
        item = observation()
        shadow = FixedImpedancePolicy(ImpedanceBounds()).propose(item)
        rejected = VelocityAdmittanceSurrogate().command(item, shadow)
        self.assertEqual(rejected.mode, "stop")
        self.assertEqual(
            dict(rejected.diagnostics)["reason"],
            "shadow_proposal_not_command_capable",
        )
        proposal = replace(shadow, shadow_only=False)
        source_hash = "b" * 64
        backend = VelocityAdmittanceSurrogate(
            step5b_scaffold_sha256=source_hash,
            step5b_activation_ready=True,
        )
        step5b = Step5bTwistInput(
            sequence=item.sequence,
            generated_at_s=item.timestamp_s,
            twist_base=(0.001, 0.0, 0.0, 0.0, 0.0, 0.0),
            scaffold_sha256=source_hash,
        )
        command = backend.command(item, proposal, step5b_input=step5b)
        self.assertEqual(command.backend_fidelity, "surrogate")
        self.assertIsNone(command.torque_nm)
        self.assertTrue(all(abs(value) <= 0.05 for value in command.qdot_rad_s))
        self.assertIn("not torque impedance", command.claim_boundary)
        self.assertFalse(command.shadow_only)

        missing = backend.command(item, proposal)
        self.assertEqual(missing.mode, "stop")
        self.assertEqual(
            dict(missing.diagnostics)["reason"], "step5b_scaffold_not_bound"
        )

        offline = VelocityAdmittanceSurrogate(
            step5b_scaffold_sha256=source_hash,
            step5b_activation_ready=False,
        ).command(item, proposal, step5b_input=step5b)
        self.assertEqual(
            dict(offline.diagnostics)["reason"],
            "step5b_scaffold_activation_not_accepted",
        )

    def test_step5b_binding_is_stable_core_not_large_bridge_and_stays_blocked(self) -> None:
        repository_root = ROOT.parents[1]
        payload = load_step5b_scaffold_binding(
            ROOT / "config/step5b_velocity_scaffold_binding.json",
            repository_root,
        )
        self.assertFalse(payload["activation_ready"])
        self.assertIn("step5b_contact_control_core.py", payload["source_path_from_repository_root"])
        self.assertNotIn("kunwei_rtde_bridge.py", payload["source_path_from_repository_root"])

    def test_capability_mux_shadow_path_is_bitwise_baseline(self) -> None:
        item = observation()
        shadow = FixedImpedancePolicy(ImpedanceBounds()).propose(item)
        baseline = (0.001, -0.002, 0.003, -0.004, 0.005, -0.006)
        mux = CapabilitySeparatedVelocityMux(
            VelocityAdmittanceSurrogate(), active_capability=False
        )
        off = mux.select(item, None, baseline)
        on = mux.select(item, shadow, baseline)
        self.assertEqual(off.qdot_rad_s, baseline)
        self.assertEqual(on.qdot_rad_s, baseline)
        self.assertEqual(on.route, "baseline_shadow_bypass")
        denied = mux.select(item, replace(shadow, shadow_only=False), baseline)
        self.assertEqual(denied.mode, "stop")
        self.assertIsNone(denied.qdot_rad_s)

    def test_torque_reference_checks_each_jacobian_row(self) -> None:
        with self.assertRaisesRegex(ValueError, "6x6"):
            torque_formula_reference(
                ((1.0,) * 6,) * 5 + ((1.0,) * 5,),
                (0.0,) * 6,
                (0.0,) * 6,
                (0.0,) * 6,
                (0.0,) * 6,
            )
        identity = tuple(
            tuple(1.0 if row == column else 0.0 for column in range(6))
            for row in range(6)
        )
        result = torque_formula_reference(
            identity,
            (1, 2, 3, 4, 5, 6),
            (0,) * 6,
            (0,) * 6,
            (1,) * 6,
        )
        self.assertEqual(result, (1, 2, 3, 4, 5, 6))

    def test_direct_torque_packet_oracle_state_transitions(self) -> None:
        state = DirectTorqueGuardState()
        first = validate_direct_torque_packet(
            self._torque_packet(1),
            state,
            release_ready=True,
            runtime_guard_ok=True,
        )
        self.assertTrue(first.accepted)

        # Contact after arming may exceed the release threshold: it is not a
        # continuously re-evaluated gate.  K may only decrease by 0.8/tick.
        second_k = (599.2, 600.0, 600.0, 30.0, 30.0, 30.0)
        second = validate_direct_torque_packet(
            self._torque_packet(2, x=0.4001, stiffness=second_k),
            first.next_state,
            release_ready=False,
            runtime_guard_ok=True,
        )
        self.assertTrue(second.accepted)
        third = validate_direct_torque_packet(
            self._torque_packet(3, x=0.4002, stiffness=second_k),
            second.next_state,
            release_ready=False,
            runtime_guard_ok=True,
        )
        self.assertTrue(third.accepted, third.reason)
        self.assertEqual(third.next_state.last_equilibrium_pose[0], 0.4002)

        cases = {
            "mixed": self._torque_packet(4, sequence_before=3),
            "frozen": self._torque_packet(3),
            "gap": self._torque_packet(5),
            "lease": self._torque_packet(4, lease=99),
        }
        for label, packet in cases.items():
            with self.subTest(label=label):
                decision = validate_direct_torque_packet(
                    packet,
                    third.next_state,
                    release_ready=False,
                    runtime_guard_ok=True,
                )
                self.assertFalse(decision.accepted)

        increase = validate_direct_torque_packet(
            self._torque_packet(4, stiffness=(600.0,) * 3 + (30.0,) * 3),
            third.next_state,
            release_ready=False,
            runtime_guard_ok=True,
        )
        self.assertFalse(increase.accepted)
        self.assertEqual(increase.reason, "stiffness_increase_or_slew_violation")

    def test_direct_torque_first_packet_requires_release_and_baseline(self) -> None:
        state = DirectTorqueGuardState()
        blocked = validate_direct_torque_packet(
            self._torque_packet(1),
            state,
            release_ready=False,
            runtime_guard_ok=True,
        )
        self.assertEqual(blocked.reason, "new_run_release_gate_failed")
        nonbaseline = validate_direct_torque_packet(
            self._torque_packet(
                1, stiffness=(599.2, 600.0, 600.0, 30.0, 30.0, 30.0)
            ),
            state,
            release_ready=True,
            runtime_guard_ok=True,
        )
        self.assertEqual(
            nonbaseline.reason, "new_run_must_restore_baseline_stiffness"
        )


if __name__ == "__main__":
    unittest.main()

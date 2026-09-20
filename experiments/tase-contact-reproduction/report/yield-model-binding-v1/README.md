# Current model matches the retained platform model

The current expanded URDF SHA-256 c0891c0df47664230c6f4f2c621c1022484131e7f7a7dba8efb2ec7c3fc4a9dc exactly matches the 2026-09-17 read-only kinematics receipt. Xacro source SHA, calibration YAML SHA and calibration identifier also match that receipt. This distinguishes the historical R003 contract mismatch from a model change since that recorded platform observation.

Recomputing FK at the retained measured joint vector gives position error 1.8360348167241365e-6 m and SO(3) rotation error 6.279839888236508e-6 rad. The retained position value is identical; its rotation computation differs by approximately 3.0e-11 rad. The fixture uses the retained TCP offset and verifies its zero rotational offset. No current robot observation was acquired.

This is one historical stationary pose, not workspace accuracy, contact-model identification, fresh payload/TCP confirmation, runtime qualification or a current physical admission. The R003 source guard remains unchanged and correctly fails. A native yield route needs its own explicitly verified model/solver binding rather than copying the outdated RNN contract or replacing its expected digest merely to make a check green.

Run check.py with the managed contact Python environment. It reads the retained receipt and present calibrated-model sources and writes result.json exclusively; preserve the existing result or use an isolated copy for a rerun. The receipt and input hashes are recorded in result.json.

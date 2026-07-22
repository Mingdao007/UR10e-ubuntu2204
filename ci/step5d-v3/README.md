# Step5d V3 OCI clean-room

Both images are pinned to the same ROS Humble base digest and consume the
repository `uv.lock`. Image build may use the network; every evidence-bearing
container runs with `--network none`, a read-only repository mount, and a
separate output mount. OCI evidence has only `HERMETIC_CI_PROVEN` authority and
never satisfies production `OFFLINE_PROVEN` or live readiness.

Hosted CI builds `Dockerfile.cpu`. The local NVIDIA freeze gate builds
`Dockerfile.cuda` and runs it with `--gpus all`; it is a CUDA functional slice,
not a production qualification or controller authorization.

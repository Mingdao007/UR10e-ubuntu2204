"""Step5b/Step5d ROS2 remote-control shadow package.

This package is offline-first. The default config and launch path never enable
robot motion, bridge startup, URScript output, or TP program execution.
"""

__all__ = ["state_machine", "replay_shadow"]

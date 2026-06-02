# Full Article Experiment Simulation Summary

Run id: `20260523T114332`
Config: `/home/andy/ur10e_ros2_ws/experiments/20260523_tase_finite_time_ur10e_mujoco_reproduction/configs/full_article_experiments.yaml`

Scope: simulated reproduction of paper Section VI trajectory families, not hardware validation.
Section V Fig.5/Fig.6 parity is provided by the MATLAB run folder, while this run covers experiment #1-#4 trajectory/force/position/orientation behavior.

| Exp | Trajectory | Material | Proposed MIAE | Baseline MIAE | Reduction | Proposed tail | Baseline tail |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| E1 | cycloid | single | 0.183852 | 0.895231 | 79.46% | 0.0800064 | 0.550018 |
| E2 | figure-eight | single | 0.183852 | 0.895231 | 79.46% | 0.0800064 | 0.550018 |
| E3 | circle | multi | 0.196207 | 1.01067 | 80.59% | 0.079994 | 0.553723 |
| E4 | cardioid | multi | 0.192234 | 0.994916 | 80.68% | 0.0800036 | 0.553726 |

Important limits:

- This is a deterministic simulation surface for the article-level experiment matrix.
- It uses paper trajectory formulas and target force values, but not real force sensor logs.
- Multi-material cases use a modeled disturbance at the configured switch time.
- Hardware claims still require UR10e/OnRobot experiment data.

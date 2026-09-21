#pragma once

#include <array>
#include <cstddef>

namespace tase_core {

using Vec6 = std::array<double, 6>;
using Mat6 = std::array<double, 36>;

struct OuterParameters final {
    double kp = 4.0;
    double ko = 5.0;
    double kf = 1.0;
    double Md_scalar = 12.0;
    double Bd_scalar = 550.0;
    double force_target_n = 5.0;
    double normal_velocity_limit_m_s = 0.003;
};

struct OuterState final {
    double normal_velocity_m_s = 0.0;
};

struct RnnConfig final {
    double epsilon = 0.022;
    double r = 1.0;
    double qdot_limit_rad_s = 0.05;
    bool lambda_update_minus = true;
};

struct StepInput final {
    Mat6 jacobian{};
    Vec6 xdot_c{};
    Vec6 omega_minus{};
    Vec6 omega_plus{};
    double dt_s = 0.002;
    bool cmd_valid = true;
};

struct StepDiagnostics final {
    Vec6 qdot{};
    Vec6 lambda{};
    Vec6 projected{};
    Vec6 residual{};
    double residual_norm = 0.0;
    bool accepted = false;
    bool rolled_back = false;
};

struct Snapshot final {
    Vec6 theta_dot{};
    Vec6 lambda{};
};

class MatureRnn final {
public:
    explicit MatureRnn(RnnConfig config = {});

    void reset() noexcept;
    void warm_start(const StepInput& input);
    [[nodiscard]] Snapshot snapshot() const noexcept;
    void restore(const Snapshot& snapshot) noexcept;
    [[nodiscard]] StepDiagnostics step(const StepInput& input);
    [[nodiscard]] const RnnConfig& config() const noexcept { return config_; }

private:
    RnnConfig config_;
    Vec6 theta_dot_{};
    Vec6 lambda_{};
};

// Paper outer-loop force-motion term, kept separate from the RNN and usable
// by both the C++ live core and the Python parameter proposer.
double step_normal_outer_loop(
    const OuterParameters& parameters,
    OuterState& state,
    double measured_normal_n,
    double measured_normal_velocity_m_s,
    double dt_s);

}  // namespace tase_core

#include "tase_core.hpp"

#include <cassert>
#include <cmath>

int main() {
    using namespace tase_core;
    StepInput input{};
    for (std::size_t i = 0; i < 6; ++i) {
        input.jacobian[i * 6 + i] = 1.0;
        input.omega_minus[i] = -0.05;
        input.omega_plus[i] = 0.05;
        input.xdot_c[i] = i == 0 ? 0.01 : 0.0;
    }
    MatureRnn solver;
    solver.warm_start(input);
    const auto first = solver.step(input);
    if (!first.accepted || std::abs(first.qdot[0]) > 0.05 + 1e-12) return 1;
    const auto before = solver.snapshot();
    input.omega_minus[0] = 0.1;
    input.omega_plus[0] = -0.1;
    const auto rejected = solver.step(input);
    if (rejected.accepted || !rejected.rolled_back) return 2;
    const auto after = solver.snapshot();
    if (before.theta_dot != after.theta_dot || before.lambda != after.lambda) return 3;
    OuterState state{};
    OuterParameters parameters{};
    const auto command = step_normal_outer_loop(parameters, state, 0.0, 0.0, 0.002);
    if (!(command > 0.0 && command <= parameters.normal_velocity_limit_m_s)) return 4;
    return 0;
}

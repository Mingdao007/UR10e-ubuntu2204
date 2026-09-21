#include "tase_core.hpp"

#include <algorithm>
#include <iomanip>
#include <iostream>

int main() {
    tase_core::MatureRnn solver;
    tase_core::StepInput input{};
    for (std::size_t i = 0; i < 6; ++i) {
        input.jacobian[i * 6 + i] = 1.0;
        input.omega_minus[i] = -0.05;
        input.omega_plus[i] = 0.05;
        input.xdot_c[i] = i == 2 ? 0.001 : 0.0;
    }
    solver.warm_start(input);
    std::size_t accepted = 0;
    double maximum_residual = 0.0;
    for (std::size_t tick = 0; tick < 5000; ++tick) {
        const auto result = solver.step(input);
        if (!result.accepted) return 2;
        ++accepted;
        maximum_residual = std::max(maximum_residual, result.residual_norm);
    }
    std::cout << std::setprecision(17)
              << "{\"schema\":\"tase.cpp-replay-v1\",\"accepted_ticks\":"
              << accepted << ",\"max_residual\":" << maximum_residual
              << ",\"qdot_limit\":0.05}\n";
    return maximum_residual < 1e-6 ? 0 : 3;
}

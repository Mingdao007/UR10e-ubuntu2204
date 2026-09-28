#include "tase_core.hpp"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <new>

namespace {

using tase_core::Vec6;

bool finite6(const double* values) {
    if (values == nullptr) return false;
    for (std::size_t i = 0; i < 6; ++i)
        if (!std::isfinite(values[i])) return false;
    return true;
}

void copy6(double* destination, const Vec6& source) {
    std::copy(source.begin(), source.end(), destination);
}

struct Solver final {
    tase_core::MatureRnn rnn;

    Solver() : rnn(tase_core::RnnConfig{.022, 1.0, .15, true}) {}
};

}  // namespace

// This narrow C ABI leaves the 512-step mature-RNN state in the process and
// performs no allocation inside a solve. A negative result rejects the whole
// call; the caller must not publish its output. The version prevents loading
// an unrelated same-named library as this candidate.
extern "C" {

int tase_rnn512_abi_version() noexcept { return 1; }

void* tase_rnn512_create() noexcept {
    try {
        return new Solver();
    } catch (...) {
        return nullptr;
    }
}

void tase_rnn512_destroy(void* handle) noexcept {
    delete static_cast<Solver*>(handle);
}

int tase_rnn512_restore(void* handle, const double* theta,
                        const double* lambda) noexcept {
    if (handle == nullptr || !finite6(theta) || !finite6(lambda)) return -1;
    tase_core::Snapshot snapshot{};
    std::copy_n(theta, 6, snapshot.theta_dot.begin());
    std::copy_n(lambda, 6, snapshot.lambda.begin());
    static_cast<Solver*>(handle)->rnn.restore(snapshot);
    return 0;
}

int tase_rnn512_snapshot(void* handle, double* theta,
                         double* lambda) noexcept {
    if (handle == nullptr || theta == nullptr || lambda == nullptr) return -1;
    const auto snapshot = static_cast<Solver*>(handle)->rnn.snapshot();
    copy6(theta, snapshot.theta_dot);
    copy6(lambda, snapshot.lambda);
    return 0;
}

int tase_rnn512_solve(void* handle, const double* jacobian,
                      const double* xdot, const double* lower,
                      const double* upper, double dt_s, int cmd_valid,
                      double* qdot, double* lambda, double* residual,
                      double* projected) noexcept {
    if (handle == nullptr || jacobian == nullptr || !finite6(xdot) ||
        !finite6(lower) || !finite6(upper) || qdot == nullptr ||
        lambda == nullptr || residual == nullptr || projected == nullptr ||
        !std::isfinite(dt_s) || dt_s <= 0.0 || dt_s >= .08 ||
        (cmd_valid != 0 && cmd_valid != 1)) return -1;
    tase_core::StepInput input{};
    for (std::size_t i = 0; i < 36; ++i) {
        if (!std::isfinite(jacobian[i])) return -1;
        input.jacobian[i] = jacobian[i];
    }
    std::copy_n(xdot, 6, input.xdot_c.begin());
    std::copy_n(lower, 6, input.omega_minus.begin());
    std::copy_n(upper, 6, input.omega_plus.begin());
    input.dt_s = dt_s;
    input.cmd_valid = cmd_valid != 0;
    auto& solver = static_cast<Solver*>(handle)->rnn;
    const auto before = solver.snapshot();
    try {
        tase_core::StepDiagnostics last{};
        for (int i = 0; i < 512; ++i) {
            last = solver.step(input);
            if (!last.accepted) {
                solver.restore(before);
                return -2;
            }
        }
        copy6(qdot, last.qdot);
        copy6(lambda, last.lambda);
        copy6(residual, last.residual);
        copy6(projected, last.projected);
        return 0;
    } catch (...) {
        solver.restore(before);
        return -3;
    }
}

}  // extern "C"

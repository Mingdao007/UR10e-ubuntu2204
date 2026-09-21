#include "tase_core.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace tase_core {
namespace {

double dot_row(const Mat6& matrix, std::size_t row, const Vec6& vector) {
    double value = 0.0;
    for (std::size_t column = 0; column < 6; ++column)
        value += matrix[row * 6 + column] * vector[column];
    return value;
}

Vec6 transpose_multiply(const Mat6& matrix, const Vec6& vector) {
    Vec6 result{};
    for (std::size_t column = 0; column < 6; ++column)
        for (std::size_t row = 0; row < 6; ++row)
            result[column] += matrix[row * 6 + column] * vector[row];
    return result;
}

bool finite(const Vec6& values) {
    for (double value : values)
        if (!std::isfinite(value)) return false;
    return true;
}

bool finite(const Mat6& values) {
    for (double value : values)
        if (!std::isfinite(value)) return false;
    return true;
}

double norm(const Vec6& values) {
    double sum = 0.0;
    for (double value : values) sum += value * value;
    return std::sqrt(sum);
}

bool solve6(Mat6 matrix, Vec6 rhs, Vec6& result) {
    for (std::size_t pivot = 0; pivot < 6; ++pivot) {
        std::size_t best = pivot;
        for (std::size_t row = pivot + 1; row < 6; ++row)
            if (std::abs(matrix[row * 6 + pivot]) >
                std::abs(matrix[best * 6 + pivot])) best = row;
        if (std::abs(matrix[best * 6 + pivot]) <= 1e-14) return false;
        if (best != pivot) {
            for (std::size_t column = 0; column < 6; ++column)
                std::swap(matrix[pivot * 6 + column], matrix[best * 6 + column]);
            std::swap(rhs[pivot], rhs[best]);
        }
        for (std::size_t row = pivot + 1; row < 6; ++row) {
            const double factor = matrix[row * 6 + pivot] / matrix[pivot * 6 + pivot];
            for (std::size_t column = pivot; column < 6; ++column)
                matrix[row * 6 + column] -= factor * matrix[pivot * 6 + column];
            rhs[row] -= factor * rhs[pivot];
        }
    }
    for (std::size_t index = 6; index-- > 0;) {
        double value = rhs[index];
        for (std::size_t column = index + 1; column < 6; ++column)
            value -= matrix[index * 6 + column] * result[column];
        result[index] = value / matrix[index * 6 + index];
    }
    return finite(result);
}

double sigr(double value, double exponent) {
    return std::pow(std::abs(value), exponent) * (value < 0.0 ? -1.0 : value > 0.0 ? 1.0 : 0.0);
}

}  // namespace

MatureRnn::MatureRnn(RnnConfig config) : config_(config) {
    if (!(std::isfinite(config_.epsilon) && config_.epsilon > 0.0) ||
        !(std::isfinite(config_.r) && config_.r > 0.0 && config_.r <= 1.0) ||
        !(std::isfinite(config_.qdot_limit_rad_s) && config_.qdot_limit_rad_s > 0.0))
        throw std::invalid_argument("invalid_tase_rnn_config");
}

void MatureRnn::reset() noexcept {
    theta_dot_.fill(0.0);
    lambda_.fill(0.0);
}

Snapshot MatureRnn::snapshot() const noexcept {
    return Snapshot{theta_dot_, lambda_};
}

void MatureRnn::restore(const Snapshot& snapshot) noexcept {
    theta_dot_ = snapshot.theta_dot;
    lambda_ = snapshot.lambda;
}

void MatureRnn::warm_start(const StepInput& input) {
    if (!finite(input.jacobian) || !finite(input.xdot_c) || !finite(input.omega_minus) ||
        !finite(input.omega_plus))
        throw std::invalid_argument("invalid_tase_rnn_input");
    Mat6 normal{};
    Vec6 rhs{};
    for (std::size_t row = 0; row < 6; ++row) {
        for (std::size_t column = 0; column < 6; ++column)
            for (std::size_t k = 0; k < 6; ++k)
                normal[row * 6 + column] += input.jacobian[row * 6 + k] *
                    input.jacobian[column * 6 + k];
        normal[row * 6 + row] += 1e-8;
        rhs[row] = input.xdot_c[row];
    }
    Vec6 solved{};
    if (!solve6(normal, rhs, solved)) throw std::invalid_argument("tase_warm_start_singular");
    lambda_ = solved;
    const Vec6 projected = transpose_multiply(input.jacobian, lambda_);
    for (std::size_t i = 0; i < 6; ++i)
        theta_dot_[i] = std::clamp(projected[i], input.omega_minus[i], input.omega_plus[i]);
}

StepDiagnostics MatureRnn::step(const StepInput& input) {
    const Snapshot before = snapshot();
    StepDiagnostics result{};
    result.qdot = theta_dot_;
    result.lambda = lambda_;
    if (!finite(input.jacobian) || !finite(input.xdot_c) || !finite(input.omega_minus) ||
        !finite(input.omega_plus) || !std::isfinite(input.dt_s) || input.dt_s <= 0.0 ||
        input.dt_s >= 0.08) {
        result.rolled_back = true;
        return result;
    }
    for (std::size_t i = 0; i < 6; ++i)
        if (input.omega_minus[i] > input.omega_plus[i]) {
            result.rolled_back = true;
            return result;
        }
    const Vec6 projected_input = transpose_multiply(input.jacobian, lambda_);
    Vec6 projected{};
    for (std::size_t i = 0; i < 6; ++i)
        projected[i] = std::clamp(projected_input[i], input.omega_minus[i], input.omega_plus[i]);
    if (input.cmd_valid) {
        for (std::size_t i = 0; i < 6; ++i) {
            const double argument = theta_dot_[i] - projected[i];
            const double delta = -(input.dt_s / config_.epsilon) * sigr(argument, config_.r);
            theta_dot_[i] = std::abs(delta) > std::abs(argument)
                ? projected[i] : theta_dot_[i] + delta;
        }
        Vec6 residual{};
        for (std::size_t row = 0; row < 6; ++row)
            residual[row] = dot_row(input.jacobian, row, theta_dot_) - input.xdot_c[row];
        for (std::size_t i = 0; i < 6; ++i)
            lambda_[i] += (config_.lambda_update_minus ? -1.0 : 1.0) *
                (input.dt_s / config_.epsilon) * residual[i];
    }
    result.qdot = theta_dot_;
    result.lambda = lambda_;
    result.projected = projected;
    for (std::size_t row = 0; row < 6; ++row)
        result.residual[row] = dot_row(input.jacobian, row, theta_dot_) - input.xdot_c[row];
    result.residual_norm = norm(result.residual);
    bool valid = finite(result.qdot) && finite(result.lambda) && finite(result.residual) &&
        result.residual_norm < 1e6;
    for (std::size_t i = 0; i < 6; ++i)
        valid = valid && result.qdot[i] >= input.omega_minus[i] - 1e-12 &&
            result.qdot[i] <= input.omega_plus[i] + 1e-12;
    if (!valid) {
        restore(before);
        result.rolled_back = true;
        result.qdot = before.theta_dot;
        result.lambda = before.lambda;
        return result;
    }
    result.accepted = true;
    return result;
}

double step_normal_outer_loop(const OuterParameters& parameters, OuterState& state,
                              double measured_normal_n, double measured_normal_velocity_m_s,
                              double dt_s) {
    if (!std::isfinite(measured_normal_n) || !std::isfinite(measured_normal_velocity_m_s) ||
        !std::isfinite(dt_s) || dt_s <= 0.0 || parameters.Md_scalar <= 0.0 ||
        parameters.Bd_scalar <= 0.0)
        throw std::invalid_argument("invalid_tase_outer_input");
    const double acceleration = (parameters.kf * (parameters.force_target_n - measured_normal_n) -
                                 parameters.Bd_scalar * measured_normal_velocity_m_s) /
                                parameters.Md_scalar;
    state.normal_velocity_m_s = std::clamp(
        state.normal_velocity_m_s + dt_s * acceleration,
        -parameters.normal_velocity_limit_m_s,
        parameters.normal_velocity_limit_m_s);
    return state.normal_velocity_m_s;
}

}  // namespace tase_core

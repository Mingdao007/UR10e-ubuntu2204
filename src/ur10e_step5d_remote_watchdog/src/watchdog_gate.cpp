// Copyright 2026 The UR10e Step5d Remote Control Authors
// Licensed under the Apache License, Version 2.0.

#include "ur10e_step5d_remote_watchdog/watchdog_gate.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace ur10e_step5d_remote_watchdog
{

WatchdogGate::WatchdogGate(const WatchdogConfig & config)
: config_(config)
{
  if (!std::isfinite(config_.max_abs_velocity_rad_s) ||
    config_.max_abs_velocity_rad_s <= 0.0 ||
    !std::isfinite(config_.max_acceleration_rad_s2) ||
    config_.max_acceleration_rad_s2 <= 0.0 ||
    !std::isfinite(config_.stale_timeout_s) || config_.stale_timeout_s <= 0.0)
  {
    throw std::invalid_argument("watchdog limits must be finite and positive");
  }
  reset(0.0);
}

void WatchdogGate::reset(const double now_s)
{
  if (!std::isfinite(now_s)) {
    throw std::invalid_argument("watchdog reset time must be finite");
  }
  pending_command_ = zero();
  last_output_ = zero();
  last_received_at_s_ = now_s;
  command_received_ = false;
  latched_ = false;
  status_ = WatchdogStatus::WAITING_ZERO;
}

void WatchdogGate::submit(const CommandEvent & event)
{
  if (latched_) {
    return;
  }
  if (event.kind == CommandEventKind::INVALID_SHAPE) {
    latch(WatchdogStatus::LATCH_SHAPE);
    return;
  }
  if (event.kind == CommandEventKind::INVALID_OVERRUN) {
    latch(WatchdogStatus::LATCH_COMMAND_OVERRUN);
    return;
  }
  if (event.kind == CommandEventKind::INVALID_NONFINITE ||
    event.kind != CommandEventKind::VALID || !std::isfinite(event.received_at_s))
  {
    latch(WatchdogStatus::LATCH_NONFINITE);
    return;
  }
  for (const double value : event.qdot) {
    if (!std::isfinite(value)) {
      latch(WatchdogStatus::LATCH_NONFINITE);
      return;
    }
    if (std::abs(value) > config_.max_abs_velocity_rad_s) {
      latch(WatchdogStatus::LATCH_QDOT_LIMIT);
      return;
    }
  }
  pending_command_ = event.qdot;
  last_received_at_s_ = event.received_at_s;
  command_received_ = true;
}

JointCommand WatchdogGate::update(const double now_s, const double period_s)
{
  if (latched_) {
    return zero();
  }
  if (!std::isfinite(now_s) || !std::isfinite(period_s) || period_s <= 0.0) {
    latch(WatchdogStatus::LATCH_NONFINITE);
    return zero();
  }
  if (!command_received_) {
    status_ = WatchdogStatus::WAITING_ZERO;
    return zero();
  }
  const double age_s = now_s - last_received_at_s_;
  if (!std::isfinite(age_s) || age_s < 0.0) {
    latch(WatchdogStatus::LATCH_NONFINITE);
    return zero();
  }
  if (age_s > config_.stale_timeout_s) {
    latch(WatchdogStatus::LATCH_STALE);
    return zero();
  }
  const double delta_limit = config_.max_acceleration_rad_s2 * period_s;
  for (std::size_t index = 0; index < kJointCount; ++index) {
    if (std::abs(pending_command_[index] - last_output_[index]) > delta_limit + 1e-12) {
      latch(WatchdogStatus::LATCH_SLEW_LIMIT);
      return zero();
    }
  }
  last_output_ = pending_command_;
  status_ = WatchdogStatus::ACTIVE;
  return last_output_;
}

WatchdogStatus WatchdogGate::status() const noexcept
{
  return status_;
}

bool WatchdogGate::latched() const noexcept
{
  return latched_;
}

void WatchdogGate::latch(const WatchdogStatus status) noexcept
{
  pending_command_ = zero();
  last_output_ = zero();
  latched_ = true;
  status_ = status;
}

JointCommand WatchdogGate::zero() noexcept
{
  return JointCommand{};
}

}  // namespace ur10e_step5d_remote_watchdog

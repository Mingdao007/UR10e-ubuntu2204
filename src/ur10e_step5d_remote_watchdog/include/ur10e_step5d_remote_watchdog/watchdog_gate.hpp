// Copyright 2026 The UR10e Step5d Remote Control Authors
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#ifndef UR10E_STEP5D_REMOTE_WATCHDOG__WATCHDOG_GATE_HPP_
#define UR10E_STEP5D_REMOTE_WATCHDOG__WATCHDOG_GATE_HPP_

#include <array>
#include <cstdint>

namespace ur10e_step5d_remote_watchdog
{

constexpr std::size_t kJointCount = 6;
using JointCommand = std::array<double, kJointCount>;

enum class WatchdogStatus : std::uint8_t
{
  WAITING_ZERO = 0,
  ACTIVE = 1,
  LATCH_STALE = 2,
  LATCH_SHAPE = 3,
  LATCH_NONFINITE = 4,
  LATCH_QDOT_LIMIT = 5,
  LATCH_SLEW_LIMIT = 6,
  LATCH_COMMAND_OVERRUN = 7,
};

enum class CommandEventKind : std::uint8_t
{
  NONE = 0,
  VALID = 1,
  INVALID_SHAPE = 2,
  INVALID_NONFINITE = 3,
  INVALID_OVERRUN = 4,
};

struct CommandEvent
{
  CommandEventKind kind{CommandEventKind::NONE};
  JointCommand qdot{};
  double received_at_s{0.0};
  std::uint64_t sequence{0};
};

struct WatchdogConfig
{
  double max_abs_velocity_rad_s{0.0};
  double max_acceleration_rad_s2{0.0};
  double stale_timeout_s{0.0};
};

class WatchdogGate
{
public:
  explicit WatchdogGate(const WatchdogConfig & config);

  void reset(double now_s);
  void submit(const CommandEvent & event);
  JointCommand update(double now_s, double period_s);

  WatchdogStatus status() const noexcept;
  bool latched() const noexcept;

private:
  void latch(WatchdogStatus status) noexcept;
  static JointCommand zero() noexcept;

  WatchdogConfig config_;
  JointCommand pending_command_{};
  JointCommand last_output_{};
  double last_received_at_s_{0.0};
  bool command_received_{false};
  bool latched_{false};
  WatchdogStatus status_{WatchdogStatus::WAITING_ZERO};
};

}  // namespace ur10e_step5d_remote_watchdog

#endif  // UR10E_STEP5D_REMOTE_WATCHDOG__WATCHDOG_GATE_HPP_

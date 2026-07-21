// Copyright 2026 The UR10e Step5d Remote Control Authors
// Licensed under the Apache License, Version 2.0.

#include <cmath>
#include <limits>

#include "gtest/gtest.h"
#include "ur10e_step5d_remote_watchdog/watchdog_gate.hpp"

namespace ur10e_step5d_remote_watchdog
{
namespace
{

WatchdogConfig config()
{
  return {0.5, 0.5, 0.010};
}

CommandEvent valid_event(const double value, const double timestamp_s, const std::uint64_t sequence)
{
  CommandEvent event;
  event.kind = CommandEventKind::VALID;
  event.qdot.fill(value);
  event.received_at_s = timestamp_s;
  event.sequence = sequence;
  return event;
}

TEST(WatchdogGate, WaitsAtExactZeroBeforeFirstCommand)
{
  WatchdogGate gate(config());
  gate.reset(10.0);

  EXPECT_EQ(gate.update(11.0, 0.002), JointCommand{});
  EXPECT_EQ(gate.status(), WatchdogStatus::WAITING_ZERO);
  EXPECT_FALSE(gate.latched());
}

TEST(WatchdogGate, PassesOnlyAValidSlewCompatibleCommand)
{
  WatchdogGate gate(config());
  gate.reset(1.0);
  gate.submit(valid_event(0.001, 1.001, 1));

  const auto output = gate.update(1.002, 0.002);

  EXPECT_DOUBLE_EQ(output[0], 0.001);
  EXPECT_EQ(gate.status(), WatchdogStatus::ACTIVE);
  EXPECT_FALSE(gate.latched());
}

TEST(WatchdogGate, StaleCommandLatchesZeroUntilReset)
{
  WatchdogGate gate(config());
  gate.reset(1.0);
  gate.submit(valid_event(0.001, 1.0, 1));

  EXPECT_EQ(gate.update(1.011, 0.002), JointCommand{});
  EXPECT_EQ(gate.status(), WatchdogStatus::LATCH_STALE);
  EXPECT_TRUE(gate.latched());

  gate.submit(valid_event(0.0, 1.012, 2));
  EXPECT_EQ(gate.update(1.012, 0.002), JointCommand{});
  EXPECT_EQ(gate.status(), WatchdogStatus::LATCH_STALE);

  gate.reset(1.013);
  EXPECT_EQ(gate.status(), WatchdogStatus::WAITING_ZERO);
  EXPECT_FALSE(gate.latched());
}

TEST(WatchdogGate, ShapeNonfiniteAndQdotViolationsLatchDistinctReasons)
{
  for (const auto expected : {
      WatchdogStatus::LATCH_SHAPE,
      WatchdogStatus::LATCH_NONFINITE,
      WatchdogStatus::LATCH_QDOT_LIMIT})
  {
    WatchdogGate gate(config());
    gate.reset(1.0);
    auto event = valid_event(0.0, 1.0, 1);
    if (expected == WatchdogStatus::LATCH_SHAPE) {
      event.kind = CommandEventKind::INVALID_SHAPE;
    } else if (expected == WatchdogStatus::LATCH_NONFINITE) {
      event.kind = CommandEventKind::INVALID_NONFINITE;
      event.qdot[0] = std::numeric_limits<double>::quiet_NaN();
    } else {
      event.qdot[0] = 0.5001;
    }
    gate.submit(event);
    EXPECT_EQ(gate.update(1.001, 0.002), JointCommand{});
    EXPECT_EQ(gate.status(), expected);
    EXPECT_TRUE(gate.latched());
  }
}

TEST(WatchdogGate, SlewViolationLatchesInsteadOfClamping)
{
  WatchdogGate gate(config());
  gate.reset(1.0);
  gate.submit(valid_event(0.0011, 1.0, 1));

  EXPECT_EQ(gate.update(1.001, 0.002), JointCommand{});
  EXPECT_EQ(gate.status(), WatchdogStatus::LATCH_SLEW_LIMIT);
  EXPECT_TRUE(gate.latched());
}

TEST(WatchdogGate, CommandOverrunLatchesInsteadOfSkippingAnIntermediateCommand)
{
  WatchdogGate gate(config());
  gate.reset(1.0);
  auto event = valid_event(0.0, 1.0, 2);
  event.kind = CommandEventKind::INVALID_OVERRUN;
  gate.submit(event);

  EXPECT_EQ(gate.update(1.001, 0.002), JointCommand{});
  EXPECT_EQ(gate.update(1.003, 0.002), JointCommand{});
  EXPECT_EQ(gate.status(), WatchdogStatus::LATCH_COMMAND_OVERRUN);
  EXPECT_TRUE(gate.latched());
}

TEST(WatchdogGate, RejectsInvalidConfiguration)
{
  EXPECT_THROW(WatchdogGate({0.0, 0.5, 0.010}), std::invalid_argument);
  EXPECT_THROW(WatchdogGate({0.5, -0.5, 0.010}), std::invalid_argument);
  EXPECT_THROW(WatchdogGate({0.5, 0.5, std::nan("")}), std::invalid_argument);
}

}  // namespace
}  // namespace ur10e_step5d_remote_watchdog

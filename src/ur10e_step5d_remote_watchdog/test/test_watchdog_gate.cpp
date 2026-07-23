// Copyright 2026 The UR10e Step5d Remote Control Authors
// Licensed under the Apache License, Version 2.0.

#include <array>
#include <cmath>
#include <cstdint>
#include <limits>

#include "ur10e_step5d_remote_watchdog/watchdog_gate.hpp"
#include "gtest/gtest.h"

namespace ur10e_step5d_remote_watchdog {
namespace {

WatchdogConfig config() { return {0.5, 0.5, 0.010}; }

CommandEvent valid_event(const double value, const double timestamp_s,
                         const std::uint64_t sequence) {
  CommandEvent event;
  event.kind = CommandEventKind::VALID;
  event.qdot.fill(value);
  event.received_at_s = timestamp_s;
  event.sequence = sequence;
  return event;
}

void emulate_controller_sequence(WatchdogGate &gate,
                                 std::uint64_t &processed_sequence,
                                 const CommandEvent &event) {
  if (event.sequence != 0 && event.sequence != processed_sequence) {
    CommandEvent submitted = event;
    if (submitted.sequence != processed_sequence + 1) {
      submitted.kind = CommandEventKind::INVALID_OVERRUN;
    }
    gate.submit(submitted);
    processed_sequence = event.sequence;
  }
}

TEST(WatchdogGate, WaitsAtExactZeroBeforeFirstCommand) {
  WatchdogGate gate(config());
  gate.reset(10.0);

  EXPECT_EQ(gate.update(11.0, 0.002), JointCommand{});
  EXPECT_EQ(gate.status(), WatchdogStatus::WAITING_ZERO);
  EXPECT_FALSE(gate.latched());
}

TEST(WatchdogGate, PassesOnlyAValidSlewCompatibleCommand) {
  WatchdogGate gate(config());
  gate.reset(1.0);
  gate.submit(valid_event(0.001, 1.001, 1));

  const auto output = gate.update(1.002, 0.002);

  EXPECT_DOUBLE_EQ(output[0], 0.001);
  EXPECT_EQ(gate.status(), WatchdogStatus::ACTIVE);
  EXPECT_FALSE(gate.latched());
}

TEST(WatchdogGate, StaleCommandLatchesZeroUntilReset) {
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

TEST(WatchdogGate, ShapeNonfiniteAndQdotViolationsLatchDistinctReasons) {
  for (const auto expected :
       {WatchdogStatus::LATCH_SHAPE, WatchdogStatus::LATCH_NONFINITE,
        WatchdogStatus::LATCH_QDOT_LIMIT}) {
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

TEST(WatchdogGate, SlewViolationLatchesInsteadOfClamping) {
  WatchdogGate gate(config());
  gate.reset(1.0);
  gate.submit(valid_event(0.0011, 1.0, 1));

  EXPECT_EQ(gate.update(1.001, 0.002), JointCommand{});
  EXPECT_EQ(gate.status(), WatchdogStatus::LATCH_SLEW_LIMIT);
  EXPECT_TRUE(gate.latched());
}

TEST(WatchdogGate,
     CommandOverrunLatchesInsteadOfSkippingAnIntermediateCommand) {
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

TEST(WatchdogGate, RepeatedSequenceIsAcceptedAsDuplicateCommand) {
  WatchdogGate gate(config());
  gate.reset(1.0);
  std::uint64_t processed_sequence = 0;
  const JointCommand expected = {{0.001, 0.001, 0.001, 0.001, 0.001, 0.001}};

  emulate_controller_sequence(gate, processed_sequence,
                              valid_event(0.001, 1.000, 1));
  EXPECT_EQ(gate.update(1.001, 0.002), expected);
  EXPECT_EQ(gate.status(), WatchdogStatus::ACTIVE);

  // Same sequence should be treated as duplicate and ignored; output remains
  // active.
  emulate_controller_sequence(gate, processed_sequence,
                              valid_event(0.003, 1.004, 1));
  EXPECT_EQ(gate.update(1.004, 0.002), expected);
  EXPECT_EQ(gate.status(), WatchdogStatus::ACTIVE);
  EXPECT_FALSE(gate.latched());

  // Next sequence should still be accepted.
  emulate_controller_sequence(gate, processed_sequence,
                              valid_event(0.001, 1.004, 2));
  EXPECT_EQ(gate.update(1.0045, 0.0005), expected);
  EXPECT_FALSE(gate.latched());
}

TEST(WatchdogGate, OutOfOrderSequenceLatchesAsCommandOverrun) {
  WatchdogGate gate(config());
  gate.reset(1.0);
  std::uint64_t processed_sequence = 0;
  const JointCommand expected = {{0.001, 0.001, 0.001, 0.001, 0.001, 0.001}};

  emulate_controller_sequence(gate, processed_sequence,
                              valid_event(0.001, 1.0, 1));
  EXPECT_EQ(gate.update(1.001, 0.002), expected);
  EXPECT_EQ(gate.status(), WatchdogStatus::ACTIVE);

  emulate_controller_sequence(gate, processed_sequence,
                              valid_event(0.001, 1.002, 2));
  EXPECT_EQ(gate.update(1.004, 0.002), expected);

  // Older sequence should not be accepted and must latch COMMAND_OVERRUN.
  emulate_controller_sequence(gate, processed_sequence,
                              valid_event(0.001, 1.003, 1));
  EXPECT_EQ(gate.update(1.006, 0.002), JointCommand{});
  EXPECT_EQ(gate.status(), WatchdogStatus::LATCH_COMMAND_OVERRUN);
  EXPECT_TRUE(gate.latched());
}

TEST(WatchdogGate, LifecycleResetClearsLatchAndAcceptsFreshCommand) {
  WatchdogGate gate(config());
  gate.reset(1.0);
  const JointCommand expected = {{0.001, 0.001, 0.001, 0.001, 0.001, 0.001}};
  gate.submit(
      valid_event(0.002, 1.0, 1)); // violates slew with max acceleration
  EXPECT_EQ(gate.update(1.001, 0.002), JointCommand{});
  EXPECT_TRUE(gate.latched());
  EXPECT_EQ(gate.status(), WatchdogStatus::LATCH_SLEW_LIMIT);

  gate.reset(2.0);
  EXPECT_FALSE(gate.latched());
  EXPECT_EQ(gate.status(), WatchdogStatus::WAITING_ZERO);

  gate.submit(valid_event(0.001, 2.0, 2));
  EXPECT_EQ(gate.update(2.001, 0.002), expected);
  EXPECT_EQ(gate.status(), WatchdogStatus::ACTIVE);
  EXPECT_FALSE(gate.latched());
}

TEST(WatchdogGate, RejectsInvalidConfiguration) {
  EXPECT_THROW(WatchdogGate({0.0, 0.5, 0.010}), std::invalid_argument);
  EXPECT_THROW(WatchdogGate({0.5, -0.5, 0.010}), std::invalid_argument);
  EXPECT_THROW(WatchdogGate({0.5, 0.5, std::nan("")}), std::invalid_argument);
}

} // namespace
} // namespace ur10e_step5d_remote_watchdog

// Copyright 2026 The UR10e Step5d Remote Control Authors
// Licensed under the Apache License, Version 2.0.

#ifndef UR10E_STEP5D_REMOTE_WATCHDOG__WATCHDOG_CONTROLLER_HPP_
#define UR10E_STEP5D_REMOTE_WATCHDOG__WATCHDOG_CONTROLLER_HPP_

#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "controller_interface/controller_interface.hpp"
#include "rclcpp/rclcpp.hpp"
#include "rclcpp_lifecycle/lifecycle_publisher.hpp"
#include "realtime_tools/realtime_buffer.hpp"
#include "std_msgs/msg/float64_multi_array.hpp"
#include "std_msgs/msg/u_int8.hpp"
#include "ur10e_step5d_remote_watchdog/watchdog_gate.hpp"

namespace ur10e_step5d_remote_watchdog {

class WatchdogController final
    : public controller_interface::ControllerInterface {
public:
  controller_interface::CallbackReturn on_init() override;
  controller_interface::InterfaceConfiguration
  command_interface_configuration() const override;
  controller_interface::InterfaceConfiguration
  state_interface_configuration() const override;
  controller_interface::CallbackReturn
  on_configure(const rclcpp_lifecycle::State &previous_state) override;
  controller_interface::CallbackReturn
  on_activate(const rclcpp_lifecycle::State &previous_state) override;
  controller_interface::CallbackReturn
  on_deactivate(const rclcpp_lifecycle::State &previous_state) override;
  controller_interface::CallbackReturn
  on_cleanup(const rclcpp_lifecycle::State &previous_state) override;
  controller_interface::return_type
  update(const rclcpp::Time &time, const rclcpp::Duration &period) override;

private:
  static double steady_now_s() noexcept;
  void write_zero() noexcept;
  void publish_status();

  std::vector<std::string> joint_names_;
  std::unique_ptr<WatchdogGate> gate_;
  realtime_tools::RealtimeBuffer<CommandEvent> command_buffer_;
  rclcpp::Subscription<std_msgs::msg::Float64MultiArray>::SharedPtr
      command_subscription_;
  rclcpp_lifecycle::LifecyclePublisher<std_msgs::msg::UInt8>::SharedPtr
      status_publisher_;
  rclcpp::TimerBase::SharedPtr status_timer_;
  std::atomic<std::uint64_t> next_sequence_{0};
  std::atomic<bool> accepting_commands_{false};
  std::uint64_t processed_sequence_{0};
  std::atomic<std::uint8_t> published_status_{
      static_cast<std::uint8_t>(WatchdogStatus::WAITING_ZERO)};
};

} // namespace ur10e_step5d_remote_watchdog

#endif // UR10E_STEP5D_REMOTE_WATCHDOG__WATCHDOG_CONTROLLER_HPP_

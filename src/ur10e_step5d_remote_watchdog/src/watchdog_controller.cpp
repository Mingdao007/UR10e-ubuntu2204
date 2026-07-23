// Copyright 2026 The UR10e Step5d Remote Control Authors
// Licensed under the Apache License, Version 2.0.

#include "ur10e_step5d_remote_watchdog/watchdog_controller.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <functional>
#include <memory>
#include <stdexcept>
#include <utility>

#include "hardware_interface/types/hardware_interface_type_values.hpp"
#include "pluginlib/class_list_macros.hpp"

namespace ur10e_step5d_remote_watchdog
{

controller_interface::CallbackReturn WatchdogController::on_init()
{
  try {
    auto_declare<std::vector<std::string>>("joints", {});
    auto_declare<double>("max_abs_velocity_rad_s", 0.0);
    auto_declare<double>("max_acceleration_rad_s2", 0.0);
    auto_declare<double>("stale_timeout_s", 0.0);
  } catch (const std::exception & error) {
    RCLCPP_ERROR(get_node()->get_logger(), "parameter declaration failed: %s", error.what());
    return controller_interface::CallbackReturn::ERROR;
  }
  return controller_interface::CallbackReturn::SUCCESS;
}

controller_interface::InterfaceConfiguration
WatchdogController::command_interface_configuration() const
{
  controller_interface::InterfaceConfiguration configuration;
  configuration.type = controller_interface::interface_configuration_type::INDIVIDUAL;
  for (const auto & joint : joint_names_) {
    configuration.names.push_back(
      joint + "/" + hardware_interface::HW_IF_VELOCITY);
  }
  return configuration;
}

controller_interface::InterfaceConfiguration
WatchdogController::state_interface_configuration() const
{
  return {controller_interface::interface_configuration_type::NONE, {}};
}

controller_interface::CallbackReturn WatchdogController::on_configure(
  const rclcpp_lifecycle::State &)
{
  const auto node = get_node();
  joint_names_ = node->get_parameter("joints").as_string_array();
  if (joint_names_.size() != kJointCount ||
    std::any_of(joint_names_.begin(), joint_names_.end(), [](const auto & value) {
      return value.empty();
    }))
  {
    RCLCPP_ERROR(node->get_logger(), "joints must contain six non-empty names");
    return controller_interface::CallbackReturn::ERROR;
  }
  auto unique_names = joint_names_;
  std::sort(unique_names.begin(), unique_names.end());
  if (std::adjacent_find(unique_names.begin(), unique_names.end()) != unique_names.end()) {
    RCLCPP_ERROR(node->get_logger(), "joint names must be unique");
    return controller_interface::CallbackReturn::ERROR;
  }

  try {
    gate_ = std::make_unique<WatchdogGate>(WatchdogConfig{
      node->get_parameter("max_abs_velocity_rad_s").as_double(),
      node->get_parameter("max_acceleration_rad_s2").as_double(),
      node->get_parameter("stale_timeout_s").as_double(),
    });
  } catch (const std::exception & error) {
    RCLCPP_ERROR(node->get_logger(), "watchdog configuration rejected: %s", error.what());
    return controller_interface::CallbackReturn::ERROR;
  }

  command_buffer_.initRT(CommandEvent{});
  next_sequence_.store(0, std::memory_order_release);
  processed_sequence_ = 0;
  command_subscription_ = node->create_subscription<std_msgs::msg::Float64MultiArray>(
    "~/commands", rclcpp::QoS(1).reliable(),
    [this](const std_msgs::msg::Float64MultiArray::SharedPtr message) {
      if (!accepting_commands_.load(std::memory_order_acquire)) {
        return;
      }
      CommandEvent event;
      event.received_at_s = steady_now_s();
      event.sequence = next_sequence_.fetch_add(1, std::memory_order_acq_rel) + 1;
      if (message->data.size() != kJointCount) {
        event.kind = CommandEventKind::INVALID_SHAPE;
      } else {
        event.kind = CommandEventKind::VALID;
        for (std::size_t index = 0; index < kJointCount; ++index) {
          event.qdot[index] = message->data[index];
          if (!std::isfinite(event.qdot[index])) {
            event.kind = CommandEventKind::INVALID_NONFINITE;
          }
        }
      }
      command_buffer_.writeFromNonRT(event);
    });
  status_publisher_ = node->create_publisher<std_msgs::msg::UInt8>(
    "~/status", rclcpp::QoS(1).reliable());
  status_timer_ = node->create_wall_timer(
    std::chrono::milliseconds(100), std::bind(&WatchdogController::publish_status, this));
  return controller_interface::CallbackReturn::SUCCESS;
}

controller_interface::CallbackReturn WatchdogController::on_activate(
  const rclcpp_lifecycle::State &)
{
  if (!gate_ || command_interfaces_.size() != kJointCount) {
    return controller_interface::CallbackReturn::ERROR;
  }
  command_buffer_.initRT(CommandEvent{});
  next_sequence_.store(0, std::memory_order_release);
  accepting_commands_.store(false, std::memory_order_release);
  processed_sequence_ = 0;
  gate_->reset(steady_now_s());
  published_status_.store(
    static_cast<std::uint8_t>(WatchdogStatus::WAITING_ZERO), std::memory_order_release);
  write_zero();
  status_publisher_->on_activate();
  accepting_commands_.store(true, std::memory_order_release);
  return controller_interface::CallbackReturn::SUCCESS;
}

controller_interface::CallbackReturn WatchdogController::on_deactivate(
  const rclcpp_lifecycle::State &)
{
  accepting_commands_.store(false, std::memory_order_release);
  write_zero();
  if (gate_) {
    gate_->reset(steady_now_s());
 }
  published_status_.store(
    static_cast<std::uint8_t>(WatchdogStatus::WAITING_ZERO), std::memory_order_release);
  if (status_publisher_) {
    status_publisher_->on_deactivate();
  }
  return controller_interface::CallbackReturn::SUCCESS;
}

controller_interface::CallbackReturn WatchdogController::on_cleanup(
  const rclcpp_lifecycle::State &)
{
  accepting_commands_.store(false, std::memory_order_release);
  write_zero();
  status_timer_.reset();
  status_publisher_.reset();
  command_subscription_.reset();
  gate_.reset();
  joint_names_.clear();
  return controller_interface::CallbackReturn::SUCCESS;
}

controller_interface::return_type WatchdogController::update(
  const rclcpp::Time &, const rclcpp::Duration & period)
{
  if (!gate_ || command_interfaces_.size() != kJointCount) {
    write_zero();
    return controller_interface::return_type::ERROR;
  }
  const CommandEvent * event = command_buffer_.readFromRT();
  if (event != nullptr && event->sequence != 0 && event->sequence != processed_sequence_) {
    CommandEvent submitted = *event;
    if (submitted.sequence != processed_sequence_ + 1) {
      submitted.kind = CommandEventKind::INVALID_OVERRUN;
    }
    gate_->submit(submitted);
    processed_sequence_ = event->sequence;
  }
  const JointCommand output = gate_->update(steady_now_s(), period.seconds());
  for (std::size_t index = 0; index < kJointCount; ++index) {
    command_interfaces_[index].set_value(output[index]);
  }
  published_status_.store(
    static_cast<std::uint8_t>(gate_->status()), std::memory_order_release);
  return controller_interface::return_type::OK;
}

double WatchdogController::steady_now_s() noexcept
{
  return std::chrono::duration<double>(
    std::chrono::steady_clock::now().time_since_epoch()).count();
}

void WatchdogController::write_zero() noexcept
{
  for (auto & command_interface : command_interfaces_) {
    command_interface.set_value(0.0);
  }
}

void WatchdogController::publish_status()
{
  if (!status_publisher_ || !status_publisher_->is_activated()) {
    return;
  }
  std_msgs::msg::UInt8 message;
  message.data = published_status_.load(std::memory_order_acquire);
  status_publisher_->publish(message);
}

}  // namespace ur10e_step5d_remote_watchdog

PLUGINLIB_EXPORT_CLASS(
  ur10e_step5d_remote_watchdog::WatchdogController,
  controller_interface::ControllerInterface)

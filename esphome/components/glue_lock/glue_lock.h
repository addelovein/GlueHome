#pragma once
#ifdef USE_ESP32
#include "glue_protocol.h"
#include "esphome/core/component.h"
#include "esphome/core/preferences.h"
#include "esphome/components/ble_client/ble_client.h"
#include "esphome/components/lock/lock.h"
#include <string>
#include <vector>

namespace esphome {
namespace glue_lock {
constexpr size_t KEY_LEN = 112;
constexpr size_t MAX_KEYS = 8;
constexpr uint32_t BANK_MAGIC = 0x474C5532;
enum class KeyState : uint8_t { AVAILABLE = 0, PENDING = 1, ACKED = 2 };
struct OfflineKey {
  uint32_t index{0};
  uint8_t key[KEY_LEN]{};
  KeyState state{KeyState::AVAILABLE};
};
struct KeyBank {
  uint32_t magic{BANK_MAGIC};
  char lock_id[37]{};
  uint64_t address{0};
  uint32_t burned_through{0};
  bool has_burned{false};
  bool paused{false};
  uint8_t count{0};
  OfflineKey keys[MAX_KEYS]{};
};
class GlueLock;
class GlueLockHub : public Component, public ble_client::BLEClientNode {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_BLUETOOTH; }
  void gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                           esp_ble_gattc_cb_param_t *param) override;
  void set_lock_id(const std::string &id) { lock_id_ = id; }
  bool add_offline_key(uint32_t index, const std::string &b64);
  bool acknowledge_burn(uint32_t index);
  bool retire_keys();
  bool resume_keys();
  int keys_remaining() const;
  std::string bank_status() const;
  void set_lock_entity(GlueLock *l) { lock_ = l; }
  void request_unlock();
  void request_lock();
 protected:
  bool save_bank_();
  void load_bank_();
  OfflineKey *next_unconsumed_key_();
  void begin_operation_(uint8_t op);
  bool send_frame_(uint8_t cmd, const uint8_t *payload, size_t len, bool raw = false);
  void on_notify_(const uint8_t *data, size_t len);
  void handle_frame_(uint8_t cmd, const uint8_t *challenge, const std::vector<uint8_t> &payload);
  void fail_op_(const char *why);
  void finish_op_(bool locked_state);
  KeyBank bank_{};
  ESPPreferenceObject pref_;
  bool bank_healthy_{true};
  std::string lock_id_;
  GlueLock *lock_{nullptr};
  uint16_t lock_char_handle_{0}, wake_char_handle_{0}, cccd_handle_{0};
  uint16_t ready_char_handle_{0};  // HOST_READY (notify) — carries the wake ack (0x01)
  bool handles_ready_{false};
  bool notify_registered_{false};
  bool wake_acked_{false};  // set when HOST_READY reports 0x01 after a wake write
  enum class Op { IDLE, WAKE, LOGIN, OPERATE } op_state_{Op::IDLE};
  uint8_t challenge_[3]{};
  uint8_t pending_operate_{glue::OP_UNLOCK};
  std::vector<uint8_t> rx_buf_;
  uint32_t op_started_ms_{0};
};
class GlueLock : public lock::Lock, public Parented<GlueLockHub> {
 public:
  void control(const lock::LockCall &call) override {
    if (call.get_state() == lock::LOCK_STATE_LOCKED) parent_->request_lock();
    else if (call.get_state() == lock::LOCK_STATE_UNLOCKED) parent_->request_unlock();
  }
};
}  // namespace glue_lock
}  // namespace esphome
#endif

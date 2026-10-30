#ifdef USE_ESP32
#include "glue_lock.h"
#include "esphome/core/log.h"
#include "esphome/core/helpers.h"
#include <cstring>
#include <algorithm>

namespace esphome {
namespace glue_lock {
static const char *const TAG = "glue_lock";
// Verified on-device (GL04A fw 0.32): LOCK_PROTOCOL is write + INDICATE (not notify),
// WAKE_UP is write-with-response, and the wake ack (0x01) arrives on HOST_READY (notify).
// All writes must be WRITE_TYPE_RSP; write-without-response is silently dropped.
// The three characteristic UUIDs are identical across hardware (GL04 and GL05,
// confirmed against the app's dex: one shared Characteristic enum). What differs
// is the SERVICE UUID that wraps them: GL04 uses fec3892f; GL05 (LOCK_V3, fw 2.x)
// uses a different one. The Android app never trips on this because rxandroidble2
// finds characteristics by char-UUID across all services; ESP-IDF's
// get_characteristic(service, char) needs the right service. So we try each
// service the app knows and use the first that yields all three chars — model
// agnostic, no per-lock config needed. The matched service is logged.
static const char *UUID_LOCK_PROTOCOL = "e4b264d7-4e56-424f-8b5c-d4089ea216ef";
static const char *UUID_WAKE_UP = "94a8dfd9-49f7-4365-95d2-d2b94624b72c";
static const char *UUID_HOST_READY = "8e939cff-3d83-4f61-8eb9-b5f25d39c15c";
// Candidate lock-service UUIDs, from the app's scan filters (s5.a domains).
// fec3892f = GL04 lock service; the others are the remaining lock-family services
// the app advertises for. First one carrying all three chars wins.
static const char *CANDIDATE_SERVICES[] = {
    "fec3892f-e506-4956-89c8-9bb5934e3121",  // GL04 lock service
    "e4b264d7-4e56-424f-8b5c-d4089ea216ef",  // lock-protocol service domain
    "d2e11ba3-e5b6-4bf8-8aa0-8af6b648b52c",  // additional lock-family service
};
// Covers on-demand connect: the lock advertises every few seconds, connect +
// service discovery ~3s, then wake/login/operate.
constexpr uint32_t OP_TIMEOUT_MS = 25000;
// Fallback only: normally the wake step completes as soon as HOST_READY reports 0x01.
constexpr uint32_t WAKE_DELAY_MS = 1000;

void GlueLockHub::setup() {
  const auto identity = "glue_bank_v2:" + lock_id_ + ":" + std::to_string(parent()->get_address());
  pref_ = global_preferences->make_preference<KeyBank>(fnv1_hash(identity));
  load_bank_();
  if (lock_) lock_->traits.set_assumed_state(true);
}
void GlueLockHub::load_bank_() {
  if (!pref_.load(&bank_)) {
    bank_ = KeyBank{};
    std::strncpy(bank_.lock_id, lock_id_.c_str(), sizeof(bank_.lock_id) - 1);
    bank_.address = parent()->get_address();
    save_bank_();
  }
  if (bank_.magic != BANK_MAGIC || bank_.count > MAX_KEYS ||
      std::strncmp(bank_.lock_id, lock_id_.c_str(), sizeof(bank_.lock_id)) != 0 ||
      bank_.address != parent()->get_address()) { bank_healthy_ = false; return; }
  for (uint8_t i = 0; i < bank_.count; ++i) {
    if (static_cast<uint8_t>(bank_.keys[i].state) > 2) bank_healthy_ = false;
    for (uint8_t j = 0; j < i; ++j)
      if (bank_.keys[i].index == bank_.keys[j].index) bank_healthy_ = false;
  }
}
bool GlueLockHub::save_bank_() {
  if (!bank_healthy_ || !pref_.save(&bank_) || !global_preferences->sync()) {
    bank_healthy_ = false;
    ESP_LOGE(TAG, "Key-bank persistence failed; operation disabled until recovery");
    return false;
  }
  return true;
}
void GlueLockHub::loop() {
  if (op_state_ == Op::IDLE) {
    // Stay disconnected while idle: saves the lock's battery and frees it for the
    // phone app. set_enabled(false) is idempotent (no-op if already disabled) and
    // tears down any stray reconnect the tracker made after a prior operation.
    if (parent()->enabled) parent()->set_enabled(false);
    return;
  }
  if (millis() - op_started_ms_ > OP_TIMEOUT_MS) { fail_op_("operation timeout"); return; }
  // While CONNECTING, wait for the link to come up (handled in the GATT events).
  if (op_state_ != Op::WAKE) return;
  // Proceed as soon as HOST_READY acks the wake (0x01); fall back to a timed wait so a
  // firmware that acks differently still advances rather than stalling to OP_TIMEOUT_MS.
  if (!wake_acked_ && millis() - op_started_ms_ < WAKE_DELAY_MS) return;
  reserve_and_login_();
}
// Reserve the lowest available key, persist the reservation, and send LOGIN. The
// reservation (PENDING + burn tombstone) is persisted BEFORE any LOGIN bytes so a
// reset, rejected login or uncertain response can never permit the key's reuse.
// Returns false (and fails the op) if no key is available or persistence fails.
bool GlueLockHub::reserve_and_login_() {
  auto *key = next_unconsumed_key_();
  if (!key) { fail_op_("no keys available"); return false; }
  key->state = KeyState::PENDING;
  bank_.burned_through = key->index;
  bank_.has_burned = true;
  if (!save_bank_()) { fail_op_("could not reserve key"); return false; }
  ++login_attempts_;
  op_state_ = Op::LOGIN;
  send_frame_(glue::CMD_LOG_IN, key->key, KEY_LEN);
  return true;
}
void GlueLockHub::dump_config() {
  ESP_LOGCONFIG(TAG, "Glue lock: %d keys, durable bank %s", keys_remaining(), bank_healthy_ ? "OK" : "FAILED");
}
int GlueLockHub::keys_remaining() const {
  if (!bank_healthy_) return 0;
  int n = 0;
  for (uint8_t i = 0; i < bank_.count; ++i)
    if (bank_.keys[i].state == KeyState::AVAILABLE) ++n;
  return n;
}
OfflineKey *GlueLockHub::next_unconsumed_key_() {
  if (!bank_healthy_ || bank_.paused) return nullptr;
  OfflineKey *best = nullptr;
  for (uint8_t i = 0; i < bank_.count; ++i) {
    auto *k = &bank_.keys[i];
    if (k->state == KeyState::AVAILABLE && (!best || k->index < best->index)) best = k;
  }
  return best;
}
bool GlueLockHub::add_offline_key(uint32_t index, const std::string &b64) {
  if (!bank_healthy_ || bank_.paused || op_state_ != Op::IDLE || index > INT32_MAX ||
      (bank_.has_burned && index <= bank_.burned_through)) return false;
  for (uint8_t i = 0; i < bank_.count; ++i)
    if (bank_.keys[i].index == index) return false;
  uint8_t raw[KEY_LEN + 4];
  if (base64_decode(b64, raw, sizeof(raw)) != KEY_LEN) return false;
  size_t slot = bank_.count;
  if (slot == MAX_KEYS) {
    for (size_t i = 0; i < MAX_KEYS; ++i)
      if (bank_.keys[i].state == KeyState::ACKED) { slot = i; break; }
    if (slot == MAX_KEYS) return false;  // never evict available or unreported keys
  } else {
    ++bank_.count;
  }
  bank_.keys[slot] = OfflineKey{};
  bank_.keys[slot].index = index;
  std::memcpy(bank_.keys[slot].key, raw, KEY_LEN);
  return save_bank_();
}
bool GlueLockHub::acknowledge_burn(uint32_t index) {
  if (!bank_healthy_ || op_state_ != Op::IDLE) return false;
  for (uint8_t i = 0; i < bank_.count; ++i) {
    auto &key = bank_.keys[i];
    if (key.index != index) continue;
    if (key.state == KeyState::ACKED) return true;
    if (key.state != KeyState::PENDING) return false;
    key.state = KeyState::ACKED;
    std::memset(key.key, 0, KEY_LEN);
    return save_bank_();
  }
  return false;
}
bool GlueLockHub::retire_keys() {
  if (!bank_healthy_ || op_state_ != Op::IDLE) return false;
  bank_.paused = true;
  for (uint8_t i = 0; i < bank_.count; ++i) {
    auto &key = bank_.keys[i];
    if (key.state != KeyState::AVAILABLE) continue;
    key.state = KeyState::PENDING;
    if (!bank_.has_burned || key.index > bank_.burned_through) bank_.burned_through = key.index;
    bank_.has_burned = true;
  }
  return save_bank_();
}
bool GlueLockHub::resume_keys() {
  if (!bank_healthy_ || op_state_ != Op::IDLE) return false;
  for (uint8_t i = 0; i < bank_.count; ++i)
    if (bank_.keys[i].state == KeyState::PENDING) return false;
  bank_.paused = false;
  return save_bank_();
}
std::string GlueLockHub::bank_status() const {
  // JSON fits HA's 255-character state limit at MAX_KEYS=8. No secret bytes.
  // Avoid reporting a reservation while LOGIN/OPERATE is still in progress.
  if (!bank_healthy_) return "error";
  if (op_state_ != Op::IDLE) return "busy";
  std::string s = "{\"lock\":\"" + lock_id_ + "\",\"pending\":[";
  bool first = true;
  for (uint8_t i = 0; i < bank_.count; ++i) if (bank_.keys[i].state == KeyState::PENDING) {
    if (!first) s += ',';
    first = false;
    s += std::to_string(bank_.keys[i].index);
  }
  s += "],\"available\":[";
  first = true;
  for (uint8_t i = 0; i < bank_.count; ++i) if (bank_.keys[i].state == KeyState::AVAILABLE) {
    if (!first) s += ',';
    first = false;
    s += std::to_string(bank_.keys[i].index);
  }
  // "stale":true means the last unlock exhausted the KEY_EXPIRED retry cap — HA
  // should refill and retry. It clears when the next operation begins.
  return s + "],\"paused\":" + (bank_.paused ? "true" : "false") +
      ",\"floor\":" + (bank_.has_burned ? std::to_string(bank_.burned_through) : "-1") +
      ",\"stale\":" + (bank_stale_ ? "true" : "false") + "}";
}
void GlueLockHub::request_unlock() { begin_operation_(glue::OP_UNLOCK); }
void GlueLockHub::request_lock() { begin_operation_(glue::OP_LOCK); }
void GlueLockHub::begin_operation_(uint8_t op) {
  if (op_state_ != Op::IDLE) return;
  // A key must be available to even start; connection is brought up on demand.
  if (!next_unconsumed_key_()) {
    if (lock_) lock_->publish_state(lock::LOCK_STATE_NONE);
    ESP_LOGW(TAG, "Cannot operate: key bank unavailable");
    return;
  }
  pending_operate_ = op;
  rx_buf_.clear();
  login_attempts_ = 0;
  bank_stale_ = false;
  op_started_ms_ = millis();
  if (lock_) lock_->publish_state(op == glue::OP_LOCK ? lock::LOCK_STATE_LOCKING : lock::LOCK_STATE_UNLOCKING);
  if (node_state == esp32_ble_tracker::ClientState::ESTABLISHED && handles_ready_) {
    start_wake_();  // already connected (e.g. back-to-back ops): go straight to wake
  } else {
    // Connect on demand: enable the client; the scanner (auto_connect) learns the
    // lock's random address type and the tracker promotes it to a connection. The
    // WRITE_DESCR_EVT handler calls start_wake_() once handles are ready.
    op_state_ = Op::CONNECTING;
    parent()->set_enabled(true);
  }
}
void GlueLockHub::start_wake_() {
  for (auto &b : challenge_) b = static_cast<uint8_t>(random_uint32());
  wake_acked_ = false;
  rx_buf_.clear();
  op_started_ms_ = millis();  // restart the clock for the wake/login/operate phase
  op_state_ = Op::WAKE;
  uint8_t wake = glue::CMD_WAKE_UP;
  send_frame_(0, &wake, 1, true);  // no fallthrough after a failed write
}
bool GlueLockHub::send_frame_(uint8_t cmd, const uint8_t *payload, size_t len, bool raw) {
  auto frame = raw ? std::vector<uint8_t>(payload, payload + len) : glue::build_frame(cmd, challenge_, payload, len);
  uint16_t handle = raw ? wake_char_handle_ : lock_char_handle_;
  for (size_t i = 0; i < frame.size(); i += 20) {
    auto err = esp_ble_gattc_write_char(parent()->get_gattc_if(), parent()->get_conn_id(), handle,
        std::min<size_t>(20, frame.size() - i), &frame[i], ESP_GATT_WRITE_TYPE_RSP, ESP_GATT_AUTH_REQ_NONE);
    if (err != ESP_OK) { fail_op_("gattc write failed"); return false; }
  }
  return true;
}
void GlueLockHub::gattc_event_handler(esp_gattc_cb_event_t event, esp_gatt_if_t gattc_if,
                                      esp_ble_gattc_cb_param_t *param) {
  switch (event) {
    case ESP_GATTC_SEARCH_CMPL_EVT: {
      handles_ready_ = false;
      notify_registered_ = false;
      // Find the service that carries all three chars. LOCK_PROTOCOL carries
      // command/response (indicate), HOST_READY the wake ack (notify), WAKE_UP is
      // write-only. Try each candidate service (GL04 fec3892f first, then the GL05
      // / lock-family services) so one firmware serves every model.
      const auto lp = esp32_ble::ESPBTUUID::from_raw(UUID_LOCK_PROTOCOL);
      const auto wu = esp32_ble::ESPBTUUID::from_raw(UUID_WAKE_UP);
      const auto hr = esp32_ble::ESPBTUUID::from_raw(UUID_HOST_READY);
      // Use the real return type of get_characteristic without naming it (the host
      // test stub and ESP-IDF use different type names for the characteristic).
      using CharPtr = decltype(parent()->get_characteristic(lp, lp));
      CharPtr lc = nullptr, wc = nullptr, rc = nullptr;
      for (const char *svc_uuid : CANDIDATE_SERVICES) {
        const auto svc = esp32_ble::ESPBTUUID::from_raw(svc_uuid);
        auto *l = parent()->get_characteristic(svc, lp);
        auto *w = parent()->get_characteristic(svc, wu);
        auto *r = parent()->get_characteristic(svc, hr);
        if (l && w && r) {
          lc = l; wc = w; rc = r;
          ESP_LOGI(TAG, "Glue characteristics found under service %s", svc_uuid);
          break;
        }
      }
      if (!lc || !wc || !rc) { ESP_LOGE(TAG, "Missing Glue characteristics (tried all candidate services)"); break; }
      lock_char_handle_ = lc->handle;
      wake_char_handle_ = wc->handle;
      ready_char_handle_ = rc->handle;
      auto *cccd = parent()->get_config_descriptor(lc->handle);
      if (!cccd) { ESP_LOGE(TAG, "Missing notification descriptor"); break; }
      cccd_handle_ = cccd->handle;
      // ESPHome's REG_FOR_NOTIFY handler writes each CCCD with the right bit (indicate for
      // LOCK_PROTOCOL, notify for HOST_READY) based on the characteristic's properties.
      // Handles become ready once LOCK_PROTOCOL's CCCD write completes (WRITE_DESCR_EVT).
      if (parent()->register_for_notify(rc->handle) != ESP_OK)
        ESP_LOGE(TAG, "HOST_READY notification registration failed");
      if (parent()->register_for_notify(lc->handle) != ESP_OK)
        ESP_LOGE(TAG, "Notification registration failed");
      break;
    }
    case ESP_GATTC_REG_FOR_NOTIFY_EVT:
      if (param->reg_for_notify.handle == lock_char_handle_)
        notify_registered_ = param->reg_for_notify.status == ESP_GATT_OK;
      break;
    case ESP_GATTC_WRITE_DESCR_EVT:
      if (param->write.handle == cccd_handle_ && notify_registered_ && param->write.status == ESP_GATT_OK) {
        handles_ready_ = true;
        node_state = esp32_ble_tracker::ClientState::ESTABLISHED;
        // On-demand connect: the link is now ready, so begin the wake step.
        if (op_state_ == Op::CONNECTING) start_wake_();
      }
      break;
    case ESP_GATTC_WRITE_CHAR_EVT:
      if (op_state_ != Op::IDLE && param->write.status != ESP_GATT_OK &&
          (param->write.handle == lock_char_handle_ || param->write.handle == wake_char_handle_))
        fail_op_("asynchronous GATT write failed");
      break;
    case ESP_GATTC_NOTIFY_EVT:
      if (param->notify.handle == lock_char_handle_) {
        on_notify_(param->notify.value, param->notify.value_len);
      } else if (param->notify.handle == ready_char_handle_) {
        // HOST_READY: a single 0x01 byte acks the wake. loop() advances WAKE -> LOGIN.
        if (op_state_ == Op::WAKE && param->notify.value_len >= 1 &&
            param->notify.value[0] == glue::CMD_WAKE_UP)
          wake_acked_ = true;
      }
      break;
    case ESP_GATTC_CLOSE_EVT:
    case ESP_GATTC_DISCONNECT_EVT:
      handles_ready_ = false;
      notify_registered_ = false;
      rx_buf_.clear();
      // A drop while still CONNECTING is expected (the lock advertises intermittently
      // and may close a half-open link); auto_connect retries on the next advert and
      // the overall timeout bounds it. Only fail a drop once we're past connect.
      if (op_state_ != Op::IDLE && op_state_ != Op::CONNECTING) fail_op_("disconnected mid-operation");
      break;
    default: break;
  }
}
void GlueLockHub::on_notify_(const uint8_t *data, size_t len) {
  if (op_state_ != Op::LOGIN && op_state_ != Op::OPERATE) return;
  if (rx_buf_.size() + len > glue::MAX_FRAME * 2) { fail_op_("receive overflow"); return; }
  rx_buf_.insert(rx_buf_.end(), data, data + len);
  while (rx_buf_.size() >= 3) {
    size_t size = rx_buf_[1] | (static_cast<size_t>(rx_buf_[2]) << 8);
    if (size < 8 || size > glue::MAX_FRAME) { fail_op_("invalid frame length"); return; }
    if (rx_buf_.size() < size) return;
    uint8_t cmd, chal[3];
    std::vector<uint8_t> payload;
    if (!glue::parse_frame(rx_buf_.data(), size, &cmd, chal, &payload)) { fail_op_("invalid frame CRC"); return; }
    rx_buf_.erase(rx_buf_.begin(), rx_buf_.begin() + size);
    handle_frame_(cmd, chal, payload);
    if (op_state_ == Op::IDLE) return;
  }
}
void GlueLockHub::handle_frame_(uint8_t cmd, const uint8_t *chal, const std::vector<uint8_t> &payload) {
  uint8_t expected = op_state_ == Op::LOGIN ? glue::CMD_LOG_IN : glue::CMD_OPERATE;
  if (cmd != expected) return;
  if (payload.empty()) { fail_op_("missing response status"); return; }
  std::memcpy(challenge_, chal, 3);
  const uint8_t status = payload[0];
  if (op_state_ == Op::LOGIN) {
    if (status == glue::R_SUCCESS) {
      op_state_ = Op::OPERATE;
      send_frame_(glue::CMD_OPERATE, &pending_operate_, 1);
      return;
    }
    // KEY_EXPIRED means another controller already burned this key. The reserved
    // key stays marked for burn-report; try the next banked key in this same
    // session, capped so a fully stale bank can't drain the whole pool at once.
    if (status == glue::R_KEY_EXPIRED && login_attempts_ < MAX_LOGIN_ATTEMPTS) {
      ESP_LOGW(TAG, "Key expired; trying next banked key (attempt %u)", login_attempts_ + 1);
      rx_buf_.clear();
      if (reserve_and_login_()) return;  // fail_op_ already ran if no key/persist failed
      return;
    }
    if (status == glue::R_KEY_EXPIRED) {
      // Exhausted the retry cap on stale keys: signal HA to refill and retry.
      bank_stale_ = true;
      fail_op_("keys stale; refill needed");
      return;
    }
    fail_op_("login rejected");
    return;
  } else if (op_state_ == Op::OPERATE) {
    bool locked = pending_operate_ == glue::OP_LOCK;
    if (status == glue::R_SUCCESS || (locked && status == glue::R_LOCK_IS_LOCKED) ||
        (!locked && status == glue::R_LOCK_IS_UNLOCKED)) finish_op_(locked);
    else fail_op_("operate rejected");
  }
}
void GlueLockHub::finish_op_(bool locked) {
  if (lock_) lock_->publish_state(locked ? lock::LOCK_STATE_LOCKED : lock::LOCK_STATE_UNLOCKED);
  op_state_ = Op::IDLE;
  rx_buf_.clear();
  handles_ready_ = false;
  // Disable the client: disconnects now and keeps it disconnected while idle
  // (saves the lock's battery, frees it for the phone app). Do NOT also call
  // disconnect() — set_enabled(false) already does, and a second call warns.
  parent()->set_enabled(false);
}
void GlueLockHub::fail_op_(const char *why) {
  ESP_LOGW(TAG, "Operation failed: %s", why);
  if (lock_) lock_->publish_state(lock::LOCK_STATE_NONE);  // unknown, not a false mechanical jam
  op_state_ = Op::IDLE;
  rx_buf_.clear();
  handles_ready_ = false;
  parent()->set_enabled(false);
}
}  // namespace glue_lock
}  // namespace esphome
#endif

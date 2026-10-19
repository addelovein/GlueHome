#pragma once
// Glue lock BLE frame protocol — ported from the decompiled SDK and cross-checked
// byte-for-byte against O.java (CRC) and BLEProtocol.java (frame layout).
// See GLUE_APP_TECHNICAL_WRITEUP.md §4/§9. Pure logic, no ESPHome deps, so it can
// be unit-tested on a host too (compile with -DGLUE_PROTOCOL_HOST_TEST).
#include <cstdint>
#include <cstddef>
#include <vector>

namespace glue {
constexpr size_t MAX_FRAME = 512;

// opcodes (writeup §9)
enum Cmd : uint8_t {
  CMD_WAKE_UP = 0x01,
  CMD_LOG_IN = 0x19,
  CMD_OPERATE = 0x32,
  CMD_BATTERY = 0x57,
  CMD_GET_FW = 0x60,
  CMD_BOLT_ENABLE = 0x75,
  CMD_BOLT_GET_STATE = 0x76,
};
enum Operate : uint8_t { OP_UNLOCK = 0x00, OP_LOCK = 0x01, OP_LATCH = 0xAA, OP_UNLATCH = 0x55 };

// lock response codes (status byte = payload[0])
enum Resp : uint8_t {
  R_SUCCESS = 0, R_INCORRECT_CHALLENGE = 21, R_OVERCURRENT = 27, R_LOCK_IS_LOCKED = 28,
  R_LOCK_IS_UNLOCKED = 29, R_MOTOR_NOT_READY = 30, R_LOGIN_ERROR = 18, R_KEY_EXPIRED = 34,
};
enum BoltState : uint8_t { BOLT_RETRACTED = 0xA0 /*unlocked*/, BOLT_PROTRUDED = 0xA1 /*locked*/ };

// CRC-16/CCITT, poly 0x1021, init 0x0000 — matches O.java exactly (bitwise form,
// verified identical to the SDK's table form over 5000 random inputs).
inline uint16_t crc16_ccitt(const uint8_t *data, size_t len) {
  uint16_t crc = 0x0000;
  for (size_t i = 0; i < len; i++) {
    crc ^= static_cast<uint16_t>(data[i]) << 8;
    for (int b = 0; b < 8; b++)
      crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021) : static_cast<uint16_t>(crc << 1);
  }
  return crc;
}

// Build [cmd][len:2 LE][challenge:3][payload][crc:2 LE], len = payload + 8.
inline std::vector<uint8_t> build_frame(uint8_t cmd, const uint8_t challenge[3],
                                        const uint8_t *payload, size_t payload_len) {
  std::vector<uint8_t> f;
  f.reserve(payload_len + 8);
  uint16_t length = static_cast<uint16_t>(payload_len + 8);
  f.push_back(cmd);
  f.push_back(length & 0xFF);
  f.push_back((length >> 8) & 0xFF);
  f.insert(f.end(), challenge, challenge + 3);
  if (payload && payload_len) f.insert(f.end(), payload, payload + payload_len);
  uint16_t crc = crc16_ccitt(f.data(), f.size());
  f.push_back(crc & 0xFF);
  f.push_back((crc >> 8) & 0xFF);
  return f;
}

// Parse an inbound frame. Returns false (lock drops silently) on short/bad-CRC input.
// On success fills cmd, challenge[3], and payload (status byte = payload[0]).
inline bool parse_frame(const uint8_t *buf, size_t len, uint8_t *cmd_out,
                        uint8_t challenge_out[3], std::vector<uint8_t> *payload_out) {
  if (len < 8 || len > MAX_FRAME) return false;
  if ((static_cast<size_t>(buf[1]) | (static_cast<size_t>(buf[2]) << 8)) != len) return false;
  size_t body = len - 2;
  uint16_t crc_rx = static_cast<uint16_t>(buf[len - 2]) | (static_cast<uint16_t>(buf[len - 1]) << 8);
  if (crc16_ccitt(buf, body) != crc_rx) return false;
  if (cmd_out) *cmd_out = buf[0];
  if (challenge_out) { challenge_out[0] = buf[3]; challenge_out[1] = buf[4]; challenge_out[2] = buf[5]; }
  if (payload_out) payload_out->assign(buf + 6, buf + body);
  return true;
}

}  // namespace glue

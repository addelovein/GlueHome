#pragma once
// Minimal platform test doubles. Tests include the production component source.
// This is NOT a substitute for a target ESPHome compile.
#include <cstdint>
#include <cstddef>
#include <string>
#include <vector>
#include <algorithm>
#include <cstring>
#include <map>
#include <cassert>
using esp_err_t=int; using esp_gattc_cb_event_t=int; using esp_gatt_if_t=int;
constexpr int ESP_OK=0, ESP_GATT_OK=0, ESP_GATT_WRITE_TYPE_NO_RSP=0, ESP_GATT_WRITE_TYPE_RSP=1, ESP_GATT_AUTH_REQ_NONE=0;
constexpr int ESP_GATTC_SEARCH_CMPL_EVT=1, ESP_GATTC_REG_FOR_NOTIFY_EVT=2, ESP_GATTC_NOTIFY_EVT=3, ESP_GATTC_CLOSE_EVT=4, ESP_GATTC_DISCONNECT_EVT=5, ESP_GATTC_WRITE_DESCR_EVT=6, ESP_GATTC_WRITE_CHAR_EVT=7;
struct esp_ble_gattc_cb_param_t {struct{uint16_t handle; int status;}reg_for_notify;struct{uint16_t handle;int status;}write;struct{uint16_t handle;uint8_t*value;size_t value_len;}notify;};
inline bool fail_write=false, fail_sync=false;
inline uint32_t clock_ms=0;
inline std::vector<std::vector<uint8_t>> writes;
inline int esp_ble_gattc_write_char(int,int,int,size_t n,uint8_t*p,int,int){if(fail_write)return 1;writes.emplace_back(p,p+n);return 0;}
#define ESP_LOGCONFIG(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
namespace esphome {
inline uint32_t millis(){return clock_ms;} inline uint32_t random_uint32(){return 1;}
inline uint32_t fnv1_hash(const std::string&s){uint32_t h=2166136261;for(auto c:s){h^=c;h*=16777619;}return h;}
inline size_t base64_decode(const std::string&s,uint8_t*p,size_t){if(s!="synthetic")return 0;std::memset(p,42,112);return 112;}
class Component {public:virtual void setup(){};virtual void loop(){};virtual void dump_config(){};virtual float get_setup_priority()const{return 0;}};
namespace setup_priority{constexpr float AFTER_BLUETOOTH=300;}
inline std::map<uint32_t,std::vector<uint8_t>> disk, staged;
class ESPPreferenceObject {public:uint32_t key=0;template<class T>bool save(T*p){staged[key]=std::vector<uint8_t>((uint8_t*)p,(uint8_t*)p+sizeof(T));return true;}template<class T>bool load(T*p){auto i=disk.find(key);if(i==disk.end()||i->second.size()!=sizeof(T))return false;std::memcpy(p,i->second.data(),sizeof(T));return true;}};
struct Preferences{template<class T>ESPPreferenceObject make_preference(uint32_t k){return {k};}bool sync(){if(fail_sync)return false;for(auto&p:staged)disk[p.first]=p.second;staged.clear();return true;}};
inline Preferences prefs;inline Preferences*global_preferences=&prefs;
template<class T>class Parented{protected:T*parent_{};};
namespace esp32_ble_tracker{enum class ClientState{IDLE,ESTABLISHED};}
namespace esp32_ble{struct ESPBTUUID{static ESPBTUUID from_raw(const char*){return {};}};}
namespace ble_client {
struct Characteristic{uint16_t handle=1;};
class BLEClient {public:int disconnects=0;int get_gattc_if(){return 0;}int get_conn_id(){return 0;}uint64_t get_address(){return 123;}int register_for_notify(int){return 0;}void disconnect(){++disconnects;}Characteristic*get_characteristic(esp32_ble::ESPBTUUID,esp32_ble::ESPBTUUID){static Characteristic c;return &c;}Characteristic*get_config_descriptor(int){static Characteristic c{2};return &c;}};
class BLEClientNode {public:virtual void gattc_event_handler(esp_gattc_cb_event_t,esp_gatt_if_t,esp_ble_gattc_cb_param_t*){};esp32_ble_tracker::ClientState node_state=esp32_ble_tracker::ClientState::ESTABLISHED;BLEClient*parent(){return &client_;}private:BLEClient client_;};
}
namespace lock{enum LockState{LOCK_STATE_NONE,LOCK_STATE_LOCKED,LOCK_STATE_UNLOCKED,LOCK_STATE_JAMMED,LOCK_STATE_LOCKING,LOCK_STATE_UNLOCKING};struct LockCall{int get_state()const{return 0;}};struct Traits{void set_assumed_state(bool){}};class Lock{public:int state=0;Traits traits;virtual void control(const LockCall&){};void publish_state(int s){state=s;}};}
}

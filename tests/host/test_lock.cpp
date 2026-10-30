#include "stub.h"
#include "../../esphome/components/glue_lock/glue_lock.cpp"
#include <iostream>
using namespace esphome;
class Hub:public glue_lock::GlueLockHub{public:
 void init(const std::string&id){set_lock_id(id);setup();ready();}
 void ready(){handles_ready_=true;node_state=esp32_ble_tracker::ClientState::ESTABLISHED;}
 bool idle(){return op_state_==Op::IDLE;}
 void reply(uint8_t cmd,uint8_t status=0){uint8_t c[3]={1,2,3};auto f=glue::build_frame(cmd,c,&status,1);on_notify_(f.data(),f.size());}
 void bytes(const std::vector<uint8_t>&f){on_notify_(f.data(),f.size());}
 void wake_ack(){wake_acked_=true;}  // simulate HOST_READY reporting 0x01
 uint8_t attempts(){return login_attempts_;}
 bool stale(){return bank_stale_;}
 // on-demand connect helpers
 void disconnected(){handles_ready_=false;node_state=esp32_ble_tracker::ClientState::IDLE;parent()->enabled=false;}
 bool connecting(){return op_state_==Op::CONNECTING;}
 bool enabled(){return parent()->enabled;}
 void connect_complete(){ // drive the real discovery path so cccd_handle_ is set, then the CCCD write
   esp_ble_gattc_cb_param_t s{};
   gattc_event_handler(ESP_GATTC_SEARCH_CMPL_EVT,0,&s);    // sets lock/wake/ready/cccd handles (stub -> cccd=2)
   esp_ble_gattc_cb_param_t r{}; r.reg_for_notify.handle=lock_char_handle_; r.reg_for_notify.status=ESP_GATT_OK;
   gattc_event_handler(ESP_GATTC_REG_FOR_NOTIFY_EVT,0,&r); // notify_registered_ = true
   esp_ble_gattc_cb_param_t p{}; p.write.handle=cccd_handle_; p.write.status=ESP_GATT_OK;
   gattc_event_handler(ESP_GATTC_WRITE_DESCR_EVT,0,&p);    // handles_ready_, ESTABLISHED, start_wake_()
 }
};
void reset(){disk.clear();staged.clear();writes.clear();fail_write=fail_sync=false;clock_ms=0;}
// Production path: wake -> HOST_READY ack advances to LOGIN (no blind delay).
void login(Hub&h){h.request_lock();h.wake_ack();h.loop();}
int main(){
 reset();Hub h;h.init("one");glue_lock::GlueLock l;h.set_lock_entity(&l);assert(h.add_offline_key(1,"synthetic"));
 fail_write=true;h.request_lock();assert(h.idle());assert(h.keys_remaining()==1);fail_write=false;
 h.ready();login(h);assert(h.keys_remaining()==0);h.reply(glue::CMD_LOG_IN);h.reply(glue::CMD_LOG_IN);
 assert(l.state!=lock::LOCK_STATE_LOCKED);h.reply(glue::CMD_OPERATE);assert(l.state==lock::LOCK_STATE_LOCKED);
 assert(h.bank_status().find("\"pending\":[1]")!=std::string::npos);
 Hub reboot;reboot.init("one");assert(reboot.keys_remaining()==0);assert(!reboot.add_offline_key(1,"synthetic"));
 assert(reboot.acknowledge_burn(1));assert(reboot.bank_status().find("\"pending\":[]")!=std::string::npos);
 Hub second;second.init("two");assert(second.keys_remaining()==0);assert(second.add_offline_key(1,"synthetic"));
 assert(reboot.add_offline_key(2,"synthetic"));login(reboot);reboot.reply(glue::CMD_LOG_IN);clock_ms+=26000;reboot.loop();assert(reboot.idle()); // > OP_TIMEOUT_MS (25s)
 Hub timeout_reboot;timeout_reboot.init("one");assert(timeout_reboot.keys_remaining()==0);
 reset();Hub full;full.init("full");for(int i=1;i<=8;++i)assert(full.add_offline_key(i,"synthetic"));
 assert(!full.add_offline_key(9,"synthetic"));full.request_lock();assert(!full.add_offline_key(9,"synthetic"));assert(full.keys_remaining()==8);
 reset();Hub bad;bad.init("bad");assert(bad.add_offline_key(1,"synthetic"));bad.request_lock();fail_sync=true;bad.wake_ack();bad.loop();assert(bad.idle());assert(writes.size()==1); // only wake; no LOGIN after failed sync
 reset();Hub invalid;invalid.init("invalid");invalid.add_offline_key(1,"synthetic");login(invalid);
 uint8_t c[3]={1,2,3},p=0;auto f=glue::build_frame(0x19,c,&p,1);f.back()^=1;invalid.bytes(f);assert(invalid.idle());assert(invalid.keys_remaining()==0);
 f=glue::build_frame(0x19,c,&p,1);f[1]=255;auto crc=glue::crc16_ccitt(f.data(),f.size()-2);f[f.size()-2]=crc&255;f.back()=crc>>8;
 assert(!glue::parse_frame(f.data(),f.size(),nullptr,nullptr,nullptr));
 reset();Hub chunks;chunks.init("chunks");chunks.add_offline_key(1,"synthetic");glue_lock::GlueLock cl;chunks.set_lock_entity(&cl);login(chunks);
 f=glue::build_frame(0x19,c,&p,1);chunks.bytes(std::vector<uint8_t>(f.begin(),f.begin()+4));chunks.bytes(std::vector<uint8_t>(f.begin()+4,f.end()));chunks.reply(glue::CMD_OPERATE);assert(cl.state==lock::LOCK_STATE_LOCKED);
 reset();Hub transfer;transfer.init("transfer");transfer.add_offline_key(1,"synthetic");transfer.add_offline_key(2,"synthetic");
 assert(transfer.retire_keys());assert(transfer.keys_remaining()==0);assert(!transfer.resume_keys());assert(!transfer.add_offline_key(3,"synthetic"));
 Hub transfer_reboot;transfer_reboot.init("transfer");assert(transfer_reboot.bank_status().find("\"paused\":true")!=std::string::npos);
 assert(transfer_reboot.acknowledge_burn(1));assert(transfer_reboot.acknowledge_burn(2));assert(transfer_reboot.resume_keys());
 assert(!transfer_reboot.add_offline_key(2,"synthetic"));assert(transfer_reboot.add_offline_key(3,"synthetic"));
 // Wake handshake: HOST_READY ack advances immediately; the timed fallback advances without one.
 reset();Hub ack;ack.init("ack");ack.add_offline_key(1,"synthetic");ack.request_lock();
 ack.wake_ack();ack.loop();assert(ack.keys_remaining()==0); // ack -> LOGIN with no time elapsed
 reset();Hub fb;fb.init("fb");fb.add_offline_key(1,"synthetic");fb.request_lock();
 fb.loop();assert(fb.keys_remaining()==1); // no ack, under fallback window: still WAKE, key not reserved
 clock_ms+=1001;fb.loop();assert(fb.keys_remaining()==0); // fallback elapsed -> LOGIN reserves key
 const uint8_t KEY_EXPIRED=34, LOGIN_ERROR=18;
 // KEY_EXPIRED on the first key -> retry the next banked key in-session -> success.
 reset();Hub retry;retry.init("retry");glue_lock::GlueLock rl;retry.set_lock_entity(&rl);
 retry.add_offline_key(1,"synthetic");retry.add_offline_key(2,"synthetic");login(retry);
 assert(retry.attempts()==1&&retry.keys_remaining()==1); // reserved key 1, key 2 still available
 retry.reply(glue::CMD_LOG_IN,KEY_EXPIRED);               // key 1 stale -> auto-retry key 2
 assert(retry.attempts()==2&&retry.keys_remaining()==0&&!retry.idle()); // now on key 2, not aborted
 retry.reply(glue::CMD_LOG_IN);retry.reply(glue::CMD_OPERATE);           // key 2 logs in, unlock ok
 assert(rl.state==lock::LOCK_STATE_LOCKED&&retry.idle()&&!retry.stale());
 assert(retry.bank_status().find("\"pending\":[1,2]")!=std::string::npos); // both burned, to report
 // All banked keys stale: retry cap exhausted -> give up, flag stale for HA, never drain >cap.
 reset();Hub drain;drain.init("drain");glue_lock::GlueLock dl;drain.set_lock_entity(&dl);
 for(int i=1;i<=5;++i)drain.add_offline_key(i,"synthetic");login(drain);
 drain.reply(glue::CMD_LOG_IN,KEY_EXPIRED);drain.reply(glue::CMD_LOG_IN,KEY_EXPIRED); // attempts 1,2 -> 3
 assert(drain.attempts()==3&&!drain.idle());
 drain.reply(glue::CMD_LOG_IN,KEY_EXPIRED);                  // 3rd rejection hits the cap
 assert(drain.idle()&&drain.stale()&&dl.state!=lock::LOCK_STATE_LOCKED);
 assert(drain.keys_remaining()==2); // exactly 3 reserved (cap), 2 untouched — pool not drained
 assert(drain.bank_status().find("\"stale\":true")!=std::string::npos);
 // A non-KEY_EXPIRED rejection must NOT retry — it isn't a stale-key problem.
 reset();Hub other;other.init("other");glue_lock::GlueLock ol;other.set_lock_entity(&ol);
 other.add_offline_key(1,"synthetic");other.add_offline_key(2,"synthetic");login(other);
 other.reply(glue::CMD_LOG_IN,LOGIN_ERROR);
 assert(other.idle()&&!other.stale()&&other.attempts()==1&&other.keys_remaining()==1); // only 1 key spent
 // On-demand connect: request while disconnected -> CONNECTING, enabled, NO key spent yet.
 reset();Hub od;od.init("od");glue_lock::GlueLock odl;od.set_lock_entity(&odl);od.add_offline_key(1,"synthetic");
 od.disconnected();assert(!od.enabled());
 od.request_lock();
 assert(od.connecting()&&od.enabled()&&od.keys_remaining()==1); // enabled client, no reservation during connect
 od.connect_complete();od.wake_ack();od.loop();   // link ready -> wake -> login reserves the key
 assert(od.keys_remaining()==0);                   // key reserved only after the link is up
 od.reply(glue::CMD_LOG_IN);od.reply(glue::CMD_OPERATE);
 assert(odl.state==lock::LOCK_STATE_LOCKED&&od.idle()&&!od.enabled()); // finished -> client disabled (idle=disconnected)
 // Connect timeout: never comes up -> fail after OP_TIMEOUT_MS, NO key spent, client disabled.
 reset();Hub ct;ct.init("ct");glue_lock::GlueLock ctl;ct.set_lock_entity(&ctl);ct.add_offline_key(1,"synthetic");
 ct.disconnected();ct.request_lock();assert(ct.connecting()&&ct.keys_remaining()==1);
 clock_ms+=26000;ct.loop();
 assert(ct.idle()&&ct.keys_remaining()==1&&!ct.enabled()); // timed out mid-connect: key NOT burned, disconnected
 // Idle loop keeps the client disabled (battery saver) even if nothing is happening.
 reset();Hub idle;idle.init("idle");idle.disconnected();assert(!idle.enabled());idle.loop();assert(!idle.enabled());
 // GL05 service fallback: the GL04 service UUID carries no chars (simulated), so
 // discovery must fall through to a later candidate service and still operate.
 reset();missing_service="fec3892f-e506-4956-89c8-9bb5934e3121"; // GL04 service "missing"
 Hub gl05;gl05.init("gl05");glue_lock::GlueLock g5;gl05.set_lock_entity(&g5);gl05.add_offline_key(1,"synthetic");
 gl05.disconnected();gl05.request_lock();gl05.connect_complete();gl05.wake_ack();gl05.loop();
 gl05.reply(glue::CMD_LOG_IN);gl05.reply(glue::CMD_OPERATE);
 assert(g5.state==lock::LOCK_STATE_LOCKED&&gl05.idle()); // found chars under a fallback service
 missing_service.clear();
 std::cout<<"PASS: wake failure, wake ack + fallback, KEY_EXPIRED retry + cap + stale signal, non-expired no-retry, on-demand connect + connect-timeout keeps key + idle-disable, GL05 service fallback, reply correlation, durable reservation/reboot, timeout, bank isolation/capacity, persistence failure, CRC/length, fragmentation, controller handoff\n";
}

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
 assert(reboot.add_offline_key(2,"synthetic"));login(reboot);reboot.reply(glue::CMD_LOG_IN);clock_ms+=16000;reboot.loop();assert(reboot.idle());
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
 std::cout<<"PASS: wake failure, wake ack + fallback, reply correlation, durable reservation/reboot, timeout, bank isolation/capacity, persistence failure, CRC/length, fragmentation, controller handoff\n";
}

# Glue Smart Lock — Android App Reverse-Engineering Writeup

**App:** `gluehome.picapau` ("Glue") v3.17.0-1574-4920ebb4 (build 3018574), Oct 2024
**Artifact analysed:** `Glue_3.17.0-1574-4920ebb4_APKPure.xapk` (split APK: base + per-language/density config splits)
**Why:** The vendor (Glue AB / gluehome.com) no longer maintains the product. The owner's Wi-Fi Hub cannot complete its cloud handshake (`ERROR_INVALID_TIMESTAMP`, Zendesk ticket #40265). Goal: understand the system well enough to interface with one's own lock independently of Glue's cloud.

> **Scope / legitimacy.** Analysis of the owner's own device and account for continued access to their own front door after vendor abandonment. Nothing here targets anyone else's lock.
>
> **Secrets note.** This document deliberately does **not** reproduce credential strings. All values live on disk already — in `decoded/res/values/strings.xml` and the named source files — so where a secret matters this writeup gives its resource name / file:line and you read it directly (e.g. `grep -n COGNITO_CLIENT_SECRET decoded/res/values/strings.xml`). See §2 for the lookup table.

> **UPDATE 2026-10-14 — read §8–§10 first for the current state.** Live traffic capture + authenticated API probing landed. Key results: **no hub on the account** (lock is local-BLE only); **key pool is hard-capped at 5** and keys are unforgeable; **firmware is not retrievable by software** (API 404 + no BLE read-back command); **trust cannot be re-rooted over BLE.** The only remaining true-independence path is **hardware (nRF52 SWD dump)** — see §10, the track to continue. Extracted live artifacts are in `extracted/` (keys, command-list, properties, user).

---

## 0. TL;DR — what matters for keeping your lock working

- There are **two independent ways to reach the lock**:
  1. **Cloud/Hub (remote):** phone → `mobile.gluehome.com` REST API → Azure (Ireland) → your Wi-Fi Hub → BLE → lock. **This is the path that is broken for you.**
  2. **Direct BLE (proximity):** phone → Bluetooth → lock, authenticated with a **single-use offline key**. The unlock itself needs **no cloud** — only *fetching* keys does.
- Your `ERROR_INVALID_TIMESTAMP` is a **cloud/hub-side** problem. The lock protocol has an explicit `DEVICE_SET_TIME (0xA1)` command and the backend issues time-bounded command-lists; the hub's clock/time handshake with the cloud is the thing failing.
- **The durable fix is to drive the lock directly over BLE from your own code**, using the recovered protocol below, and to **extract offline keys while your account still provisions them**.
- The BLE auth is **not** app-layer encrypted. Security rests on (a) single-use offline keys minted cloud-side and (b) a lock-echoed 3-byte challenge. There is **no permanent offline master secret in the app** — so once the key pool drains and the cloud won't refill it, offline control stops too. **Extract a key batch now.**

---

## 1. Package / architecture map

Kotlin, R8-obfuscated, but the app's own packages kept their names. Three layers:

| Layer | Package | Role |
|---|---|---|
| **App / UI** | `picapau/*` | Single-activity (Jetpack Navigation) UI, ~20 feature modules, ViewModels, workers, geofence/BLE-scan auto-unlock |
| **Cloud API client** | `gluehome.gluemobileapi` + `picapau/data/network` | Retrofit/OkHttp/Moshi REST client against `mobile.gluehome.com` |
| **BLE lock SDK** | `gluehome.gluetooth` | RxAndroidBle2 + Nordic DFU; the actual lock/hub protocol |

- **DI:** Koin (modules in `p184t7/`). **Presentation:** MVVM (Fragments + ViewModels + LiveData/StateFlow), Tinder StateMachine for install flows. **No Compose.**
- **Concurrency is mixed:** coroutines/Flow in REST + repository layer; **RxJava2 (RxAndroidBle2)** in BLE layer; bridged via `kotlinx-coroutines-rx2`.
- **Application class:** `picapau.PicaPauApplication`. **Launcher (only activity):** `picapau.features.MainActivity`.
- **Config constants:** `APP_FLAVOR=prod`, `IS_DEBUG=false`, `NETWORK_TIMEOUT=15s`, `CACHE_SIZE=10MB`.

### Deep link (registration/invite)
`gluecontrol://www.gluehome.com/in-app-reg/register-user?emailaddress=&phonenumber=&inviteid=` (and the `https://` variant). `MainActivity.g1()` routes these into registration via `InviteParams`.

---

## 2. Backend / environment & embedded secrets

**Single production environment** (no staging/dev host baked into the binary).

| Item | Value / where to read it |
|---|---|
| REST API base | `https://mobile.gluehome.com/api/` (resource `BACKEND_URL`) |
| Auth | **AWS Cognito**, region `eu-west-1` → `cognito-idp.eu-west-1.amazonaws.com` |
| Cognito pool / client id / client secret | resources `COGNITO_POOL_ID`, `COGNITO_CLIENT_ID`, `COGNITO_CLIENT_SECRET` → `grep -n COGNITO decoded/res/values/strings.xml` |
| OAuth web client | resource `default_web_client_id` |
| Firebase project | `glue-prd` (see `google_app_id`, `gcm_defaultSenderId`) |
| Google Maps / Firebase API keys | resources `MAPS_API_KEY`, `google_api_key` |
| Mixpanel token | resource `MIXPANEL_TOKEN` |
| Sematext Logsene token / receiver | manifest meta-data `LogseneAppToken` / `LogseneReceiverUrl` (`grep -n Logsene decoded/AndroidManifest.xml`) |
| **Seguro prefs password** (hardcoded) | `decompiled/sources/p184t7/C2181o0.java:546` (literal passed to `.d(...)`) |

**Lookup commands:**
```
grep -nE 'BACKEND_URL|COGNITO_|MAPS_API_KEY|MIXPANEL_TOKEN|google_app_id|gcm_defaultSenderId|default_web_client_id' decoded/res/values/strings.xml
grep -n Logsene decoded/AndroidManifest.xml
sed -n '546p' decompiled/sources/p184t7/C2181o0.java
```

### Security observations (relevant to you, and in general)
- **Your login password is persisted on-device** so the app can silently re-mint Cognito tokens. Stored via `cesarferreira/seguro` (file `cesarferreira.seguro.library`), "protected" by the **hardcoded** password above — effectively plaintext-equivalent. Key `STUFF` = password, `USER_ID` = user id (`SecureUserDetails`).
- **Cognito client secret is shipped in the APK** — notable exposure, but it's *your* app's secret; it's what you'd use to authenticate against the API yourself.
- **Offline lock keys are stored unencrypted** in a plain Room DB (no SQLCipher) — see §4.
- Prod OkHttp logging = HEADERS level, `Authorization` redacted (low risk); BODY in debug builds.

---

## 3. Cloud REST API catalog

Retrofit 2 + OkHttp + Moshi. Base = `BACKEND_URL`. Auth interceptor adds `Authorization: Bearer <Cognito access-token JWT>` + `Accept: application/json` **only** when the request carries the internal header `glue-requires-authorization: true`. Token from `CognitoTokenProvider` (in-memory cache, refreshed via the stored password).

**Unauthenticated endpoints:** `GET /api/v1/users?q=`, `GET /api/v1/privacy-policy`, all `/api/v1/registrations/*`.

### Locks & devices
```
GET    /api/v1/locks                                       → List<LockModel>
GET    /api/v1/locks/{id}                                  → LockModel
POST   /api/v1/locks/{id}/events                           (CreateLockEventModel)

POST   /api/v1/glue-osm-devices                            (CreateLockModel) → CreatedLockModel
PATCH  /api/v1/glue-osm-devices/{id}                       (UpdateLockModel)
DELETE /api/v1/glue-osm-devices/{id}
POST   /api/v1/glue-osm-devices/{id}/operations           (CreateGlueOsmLockOperationModel) → OperationModel
GET    /api/v1/glue-osm-devices/{id}/command-list          → LockCommandListModel
GET    /api/v1/glue-osm-devices/{id}/command-list/version  → LockCommandListVersionModel
GET    /api/v1/glue-osm-devices/{id}/keys                  → List<LockOfflineKeyModel>   ★ offline keys
DELETE /api/v1/glue-osm-devices/{id}/keys/{key}
GET    /api/v1/glue-osm-devices/{id}/firmware-update       → FirmwareUpdateModel
```

### Hubs
```
GET    /api/v1/hubs                                   → List<HubModel>
POST   /api/v1/hubs                                   (CreateHubModel) → RegisterHubModel
GET    /api/v1/hubs/{id}                              → HubModel
DELETE /api/v1/hubs/{id}
PUT    /api/v1/hubs/{id}/locks/{lockId}
POST   /api/v1/hubs/{id}/operations                   (CreateHubOperationModel) → OperationModel   ★ remote unlock
GET    /api/v1/hubs/{id}/operations/{operationId}      → OperationModel
GET    /api/v1/hubs/{id}/firmware-update               → HubFirmwareUpdateModel
```

### Properties / doors / access
```
GET    /api/v1/properties         POST (CreatePropertyModel)   GET /{id}   PATCH /{id} (PatchPropertyModel)
GET    /api/v1/doors/{id}         PATCH /{id} (UpdateDoorModel)
POST   /api/v1/doors/{id}/users   (AddUserToDoorModel)         DELETE /{id}/users/{userId}
POST   /api/v1/doors/{id}/invites (InviteUserToDoorModel)      DELETE /{id}/invites/{inviteId}
```

### Events / deliveries / init / pin-pad
```
GET  /api/v1/events?doorId=&propertyId=&type=&page=&count=        → List<EventModel>
GET  /api/v1/deliveries?isActive=&lockId=&page=&count=            → List<PartialDeliveryModel>
GET  /api/v1/deliveries/{id}  /{id}/timeline  /{id}/videos/{videoName}/link
POST /api/v1/deliveries/{id}/approve | /reject | /revoke          PUT /{id}/pin-code
GET  /api/v1/deviceinitialisation?isActive=&lockId=&page=&count=
POST /api/v1/pin-pad-devices   PATCH /{id}   DELETE /{id}
```

### Lock users / devices / messages / notifications / user
```
GET/POST /api/v1/locks/{lockId}/userdevices       GET/PATCH/DELETE /api/v1/lockusers/{lockUserDeviceId}
GET  /api/v1/userdevices/users/{userId}
GET/POST /api/v1/userdevices/{deviceId}/messages  PATCH .../messages/{messageId}
POST /api/v1/userdevices/platform/messages?lockId=
GET  /api/v1/notifications
GET/PATCH/DELETE /api/v1/user   POST /api/v1/user/email | /phone-number | /phone-number/validate
PUT  /api/v1/user/password      PUT /api/v2/user/icon (image upload — only v2 route)
POST/DELETE /api/v1/user/push-notification-registrations
GET  /api/v1/users?q=                         [NO auth]
POST /api/v1/registrations | /validate | PATCH /{id} | POST /{id}/activate   [NO auth]
GET  /api/v1/privacy-policy                   [NO auth]
```

**Model-only, not wired in this build:** the whole **NSM lock** family (`glue-nsm-devices` — never called), `Subscription*` (only fields on LockModel/UserModel), `AccessRecovery*`, `OwnershipTransferOptions`, `Batch*Model`.

---

## 4. The BLE lock protocol (the important part)

**Stack:** RxAndroidBle2 + Nordic DFU. No raw GATT, **no app-initiated bonding/MTU negotiation** — link security left to lock firmware. Writes chunked at **20 bytes**.

### GATT UUIDs
| Kind | UUID | Meaning |
|---|---|---|
| Service | `E4B264D7-4E56-424F-8B5C-D4089EA216EF` | **Lock service** |
| Service | `D2E11BA3-E5B6-4BF8-8AA0-8AF6B648B52C` | **Hub service** |
| Service | `0000FE59-...` | Nordic **DFU** (firmware) |
| Char | `LOCK_PROTOCOL` = `E4B264D7-4E56-424F-8B5C-D4089EA216EF` | lock command/response |
| Char | `HUB_PROTOCOL` = `D2E11BA5-E5B6-4BF8-8AA0-8AF6B648B52C` | hub command/response |
| Char | `WAKE_UP` = `94A8DFD9-49F7-4365-95D2-D2B94624B72C` | wake (raw `0x01`) |
| Char | `HOST_READY` = `8E939CFF-3D83-4F61-8EB9-B5F25D39C15C` | |

### Frame format (`BLEProtocol$Packet`)
```
[command:1][length:2 LE][challenge:3][payload:N][crc16:2 LE]
```
- `length = payload.length + 8` (little-endian).
- **CRC-16/CCITT**, poly `0x1021`, init `0x0000`, appended little-endian (`O.java`). Inbound frames failing length/CRC are **silently dropped**.
- Response status byte = `payload[0]`, decoded via `LockByteResponseCode` / `HubByteResponseCode`.
- **Challenge:** 3 bytes. The lock **issues a fresh challenge in every reply**; client echoes it in the next frame (anti-replay). First frame seeds from `java.util.Random`, then immediately overwritten by the lock's value. No MAC over the frame. Wrong value → `INCORRECT_CHALLENGE (21)`.

### Opcodes (`DeviceCommand`)
| Cmd | Byte | | Cmd | Byte |
|---|---|---|---|---|
| WAKE_UP | 0x01 | | GET_FIRMWARE_VERSION | 0x60 |
| LOG_IN | 0x19 | | FIRMWARE_UPDATE_PACKET | 0x61 |
| OPERATE | 0x32 | | FIRMWARE_UPDATE_COMMIT | 0x63 |
| UPDATE_COMMAND_LIST | 0x42 | | FIRMWARE_ENTER_DFU_MODE | 0x65 |
| COMMIT_COMMAND_LIST | 0x43 | | **DEVICE_SET_TIME** | **0xA1 (-95)** |
| GET_ACCESS_LIST_VERSION | 0x56 | | DEVICE_GET_TIME | (adjacent) |
| BATTERY_LEVEL | 0x57 | | DEVICE_SET_LATCH_MODE | 0xB2 |

Plus bolt/door position + calibration (~0x70–0x7C) and hub Wi-Fi/MQTT provisioning (0x07–0x0F).

**OPERATE sub-commands (`OperateCommand`):** `UNLOCK=0x00`, `LOCK=0x01`, `LATCH=0xAA`, `UNLATCH=0x55`.

### Unlock sequence (from `FrameBuilder`)
1. **Wake:** raw `0x01` → `WAKE_UP` characteristic.
2. **Login:** `LOG_IN (0x19)`, payload = **offline access key** (base64-decoded) → `FrameBuilder.p(key)`.
3. **Operate:** `OPERATE (0x32)` payload `0x00` = unlock (`FrameBuilder.r()`); `0x01` = lock (`.o()`).

`FrameBuilder.D(epoch)` builds `DEVICE_SET_TIME` carrying a 4-byte epoch — **this is the clock the lock runs on, and the thing your hub/cloud timestamp error is about.**

### Authentication = cloud-provisioned single-use offline keys
- `KeyRepository`: `getNextAccessKey()`, `consumeKey()`, `getLockCommandList()`, `eagerlyDownloadAndStoreKeys()`, `availableKeysCount()`.
- `LockKey` = `{ UUID lockId, int keyIndex, String offlineKeyInBase64 }`.
- Each successful login **consumes** that key; app refills the pool from `GET /glue-osm-devices/{id}/keys` when low (`KeyBurnerWorker` reports a burned index).
- The **command/access list** is a server-produced opaque blob relayed to the lock (`UPDATE_COMMAND_LIST` chunks + `COMMIT_COMMAND_LIST`), version-gated.
- Lock response codes include `KEY_EXPIRED`, `GLUE_ERR_ACCESS_SCHEDULE_NOT_FOUND`, `INCORRECT_CHALLENGE`, `LOGIN_ERROR` — **keys are time/schedule-bounded server-side.**

### Key storage (unencrypted)
Room DB `gluetooth_db` (`GluetoothDatabase`, plain `FrameworkSQLiteOpenHelperFactory`, **no SQLCipher**):
- **`access_keys`** `(lock_id, key_index, lock_serial_number, key TEXT /*base64 plaintext*/, consumed INTEGER, PK(lock_id,key_index))` — entity `o5/AccessKeyEntity`.
- **`command_lists`** `(lock_id, version, lock_serial_number, command_list TEXT)`.
- **`known_lock_list`** includes `encryption_pk TEXT` (per-lock public key) + `firmware_version`.
- `smart_locks_list`, `proxy_queue` (offline command queue).

> On a rooted phone or via `adb backup`/extraction, `access_keys` is readable plaintext base64. That pool is your offline-unlock ammunition.

### Hub / Wi-Fi provisioning (BLE, over `HUB_PROTOCOL`)
`GET_WIFI_SCAN_RESULTS(0x07)`, `SET_SSID(0x08)`, `SET_NETWORK_PASSWORD(0x09, plaintext on wire)`, `SET_ENCRYPTION_TYPE(0x0A)`, `TEST_CONNECTION(0x0B)`, `SET_MQTT_SERVER/USER/PASSWORD(0x0D-0x0F)`. The Hub can be re-provisioned over BLE without the cloud — useful if you ever stand up your own MQTT endpoint.

### Firmware (DFU)
Nordic DFU (`v2/dfu/DfuService`). Lock enters DFU with `FIRMWARE_ENTER_DFU_MODE (0x65)`, then standard Nordic zip over service `0xFE59`.

### Remote ("connectionless") operation = cloud relay
`POST /api/v1/hubs/{id}/operations` with command strings (`unlock`/`lock`/`latch`/`unlatch`) + `validFrom`/`validUntil`; backend relays to the Hub over MQTT, Hub drives BLE. `HubRequestDTO`/`HubResponseDTO`.

---

## 5. Auto-unlock flow (geofence → scan → BLE)

1. **Setup** (`AutoUnlockFragment`/`autounlock/l`): pick home point, default radius **200 m**, build `GeofenceData(id=lockId, deviceName=lockSN, ENTER)`, register with GMS `GeofencingClient`, persist to prefs. Requires **background location**.
2. **ENTER** → `GeofenceBroadcastReceiver` (ignores triggers within 60s of boot re-registration) → `GeofenceIntentService`.
3. **Background BLE scan** filtered by `deviceName == lock serial number` (RxAndroidBle background scanner → `ScanReceiver` PendingIntent).
4. **Scan hit** → `ScanReceiver` → resolves `u7.b` ViewModel → `lockRepository.autoUnlock(300000L)`.
5. **BLE** (`GlueLockRepositoryImpl.autoUnlock`): connect → read battery → `AUTO_UNLOCK` (the wake/login/operate sequence). Logged by `LogLockOperationWorker`.

**Known weaknesses (why it's flaky):** `ScanReceiver` PendingIntent is static and nulled after process death (results silently dropped); cached with the first lock id (**one lock only**); **no RSSI threshold and no re-check that you're inside the zone** at scan time.

---

## 6. Components, storage, push

- **Services:** `FirebaseNotificationService` (FCM), `DfuService`, geofence/location JobIntentServices, `LocationTrackingRestartService` (sticky foreground — **no `foregroundServiceType` declared**, can throw on Android 14+).
- **Workers:** `FCMTokenRegistrationWorker`, `UnregisterFCMWorker`, `ReportLockInfoWorker` (battery/fw/cmd-list version), `KeyBurnerWorker` (reports consumed key index), `LogLockOperationWorker`, `ReportBoltPositionWorker`, `UploadProfilePhotoWorker`.
- **Local storage:** default SharedPreferences (`gluehome.picapau_preferences.xml`); Seguro prefs (login creds); PaperDB "Glache" cache; Room `gluetooth_db` (keys); Fetch2 download DB (firmware).
- **Push types (FCM):** 33 distinct event UUIDs in `PushNotificationEvent` — hub connected/paired/firmware, remote lock/unlock success/fail, access created/withdrawn/enabled/disabled, door status, delivery approval/pin/cancel/expire, auto lock/unlock. FCM only acts when authenticated; events fan out on an in-app `PublishSubject` bus.
- **Third-party SDKs:** Firebase (Messaging/Crashlytics/Analytics/RemoteConfig/Installations), Mixpanel (profile + Wi-Fi SSID/signal), Sematext Logsene (device/lock SN/uuid/firmware), AWS Cognito, Retrofit/OkHttp/Moshi, RxAndroidBle2 + Nordic DFU, Google Maps + Play Location, ExoPlayer (instruction videos in `res/raw/`), Lottie/Glide/Picasso, zxing (QR serials), Dexter, Fetch2, Koin, Tinder StateMachine.

---

## 7. Your specific problem (`ERROR_INVALID_TIMESTAMP`) and the paths forward

**Diagnosis from emails + code:** your Hub is on the network and reaches Glue's Azure API in Ireland, but *"the response the device gets from the API isn't what it wants."* On Android it surfaced as **`ERROR_INVALID_TIMESTAMP`**. The lock/hub run on a clock set via `DEVICE_SET_TIME (0xA1)`, and the backend issues **time-bounded** command-lists/keys (`validFrom`/`validUntil`, `KEY_EXPIRED`). This is a **cloud/hub time-handshake failure**, not a networking fault on your side — consistent with an abandoned backend drifting out of spec. Support couldn't fix it and offered an RMA.

### Option A — Direct BLE control (recommended, cloud-independent)
Standalone script (e.g. Python + `bleak`, or a small Android/Kotlin tool) implementing §4: wake → login(offline key) → operate(unlock). Bypasses the Hub **and** the timestamp bug entirely.
- **Prerequisite:** extract your `access_keys` (+ `command_lists`) from Room DB `gluetooth_db`, or pull a fresh batch via `GET /glue-osm-devices/{id}/keys` while your account still authenticates.
- **Caveat:** keys are **single-use and finite**. Harvest a large batch now; once the cloud won't refill, offline unlocks are limited to what you've stored. (Whether the lock accepts a re-set clock / fresh command-list offline is firmware-dependent — worth testing on your own unit.)

### Option B — Revive the cloud/hub path
Use the recovered REST API + your Cognito creds (pool/client id/secret in §2, your stored password) to log in yourself, reproduce the hub operation call, and inspect exactly which timestamp the server rejects (`POST /hubs/{id}/operations`, command-list endpoints). Potentially fixable if it's clock-skew/format; fragile because it still depends on Glue's live backend.

### Option C — Extract keys first (do this regardless)
The enabling step for A. Root/`adb` extraction of `gluetooth_db`, or authenticated `/keys` pulls. The one time-sensitive action — do it while the account still works.

---

## 8. LIVE FINDINGS (2026-10-14) — captured traffic + authenticated API probing

Source: Charles Proxy captures of the **iOS** Glue app (lock-ios v3.6.0) on the owner's account, plus `curl` replays with a live Cognito Bearer token (iOS client id `4og3rhnsqhge1ovnevggbp5j41`; same REST API as Android). Extracted artifacts saved under `extracted/`.

### Account / lock identity (confirmed live)
| Item | Value |
|---|---|
| Property | "EagleRoad", postcode 136 37, SE (owner = Adde Lovein) |
| User id (`sub`) | `<YOUR_COGNITO_SUB_UUID>` |
| Door / Lock id | `<YOUR_LOCK_ID>` ("Entrance") |
| **Lock serial (= BLE scan name)** | **`GL04A.DLXXXX.XXXX`** |
| Firmware / hardware | fw **`0.32`**, hw **`3`** |
| Command-list version | **72** |
| **Hubs on account** | **`[]` — none.** Remote/cloud operation was never set up; all door events are `localUnlock` (direct BLE). This reframes the original `ERROR_INVALID_TIMESTAMP`: there is no hub registered now, so the lock is driven purely by local BLE. |

> **PII note:** `extracted/properties.json` also contains a second keyholder's name/email/phone. Keep `extracted/` private.

### The key pool is HARD-CAPPED at 5 — you cannot stockpile (critical)
`GET /api/v1/glue-osm-devices/{id}/keys` returns **exactly 5 keys** (indexes 411–415 at capture time). Verified live that query params do **not** widen it:
```
?count=100  -> 5    ?take=100 -> 5    ?page=0&count=100 -> 5    ?amount=50 -> 5
```
Re-calling returns the **same** 5 — the server maintains a rolling 5-deep buffer and only mints the **next** index after one is consumed and the burn is reported (`KeyBurnerWorker` → server advances the window). Keys are 112-byte opaque crypto blobs (full 0–255 byte range, every pair differs in all 112 bytes, no shared header/footer, no counter) — i.e. **server-signed/encrypted access grants, not forgeable or derivable from anything on the device.**

**Consequence:** while Glue's API is up you have effectively unlimited unlocks (burn → refill). **The moment Glue's API goes down you have only the 5 keys in hand, then the offline path is dead.** You cannot pre-buy years of keys — the cap prevents it.

### Firmware is NOT retrievable by software — both routes closed
1. **Via the API:** `GET /glue-osm-devices/{id}/firmware-update` → **`404 "No firmware update available"`** (your lock is already current at 0.32; the endpoint only serves *newer* images). Query hints (`currentVersion=0.0`, etc.) all 404. `/glue-osm-devices/firmwares` returns **405** (path exists but GET not allowed — an admin/upload route; deeper enumeration was deliberately not pursued).
2. **Via BLE:** the lock's complete 36-command protocol (§9) was audited — **there is no read-memory / read-flash / dump / debug / factory-reset command.** Every `GET_*` returns a small structured value. Nordic DFU is **write-only** (no read-back); entering the bootloader does not expose the image. `GET_FIRMWARE_VERSION` returns the string "0.32", not bytes.

### Re-rooting trust over BLE is NOT possible
- The lock trusts **server-signed** access/command lists. `UPDATE_COMMAND_LIST (0x42)` / `COMMIT_COMMAND_LIST (0x43)` merely courier a blob **Glue's backend signed**; the app can't forge one. There is **no "set/rotate the lock's trusted key" command** in the protocol.
- `encryption_pk` (in local table `known_lock_list`) is the **lock's public key**, cached read-only so the app can encrypt payloads to it. No protocol path writes/replaces the lock's keypair.
- Buttonless **Secure DFU** is enabled (`setUnsafeExperimentalButtonlessServiceInSecureDfuEnabled(true)`, `GlueLock.java:2171-2174`) — "Unsafe/Experimental" refers to the un-bonded buttonless *entry* characteristic, **not** to images being unsigned. Nordic Secure DFU verifies an ECDSA-P256 signature, so pushing custom firmware still needs Glue's DFU signing key — unless the device bootloader is misconfigured (not determinable from the app).

**Net:** every software path (app / REST API / BLE protocol) converges on the same wall — **Glue's private signing keys are the root of trust and exist only on Glue's servers.** No app/API/BLE work forges or relocates them.

---

## 9. Complete BLE command protocol (verified, all 36 opcodes)

Audited against `DeviceCommand.java` and the Kotlin `@Metadata` name list (1:1, nothing hidden). Hex = signed Java byte `&0xFF`. "R" = reads a value back.

| Opcode | Byte | FrameBuilder | Notes |
|---|---|---|---|
| UNKNOWN_COMMAND | 0x00 | — | |
| WAKE_UP | 0x01 | `t()` | raw single byte `0x01`, no challenge |
| LOG_IN | 0x19 | `p(key)` | payload = offline access key |
| OPERATE | 0x32 | `o()` lock / `r()` unlock | sub: UNLOCK=0x00, LOCK=0x01, LATCH=0xAA, UNLATCH=0x55 |
| UPDATE_COMMAND_LIST | 0x42 | `s(list)` | pushes server-signed access list |
| COMMIT_COMMAND_LIST | 0x43 | `k()` | commits it |
| GET_ACCESS_LIST_VERSION | 0x56 | `m()` | R: access-list version int |
| BATTERY_LEVEL | 0x57 | `j()` | R: battery |
| GET_FIRMWARE_VERSION | 0x60 | `l()` | R: version string |
| CALIBRATION_START | 0x70 | `i()` | |
| CALIBRATION_ABORT | 0x71 | (enum only) | |
| CALIBRATION_NETWORK_TEST | 0x72 | `f()` | self-test pass/fail |
| CALIBRATION_SET_POSITION | 0x73 | `e()/g()/h()` | |
| BOLT_POSITION_ENABLE | 0x75 | `x()` | |
| BOLT_POSITION_GET_STATE | 0x76 | `z()` | R: bolt state |
| DOOR_POSITION | 0x78 | `w()/u()/v()` | enable/disable |
| DOOR_POSITION_GET_STATE | 0x79 | `C()` | R: open/closed |
| DOOR_POSITION_GET_ANGLE | 0x7A | `B()` | R: hinge angle |
| DOOR_POSITION_CALIBRATE | 0x7C | `d()` | |
| FIRMWARE_UPDATE_PACKET | 0x61 | — | not sent by app (DFU lib handles it) |
| FIRMWARE_UPDATE_COMMIT | 0x63 | — | not sent by app |
| FIRMWARE_ENTER_DFU_MODE | 0x65 | `y()` | reboots lock into Nordic bootloader |
| DEVICE_GET_STATUS | 0x80 | — | defined, never sent |
| DEVICE_GET_TIME | 0xA0 | `A()` | R: time |
| DEVICE_SET_TIME | 0xA1 | `D(epoch)` | 4-byte epoch LE |
| DEVICE_ENABLE_EVENTS | 0xA2 | — | defined, never sent |
| HOLIDAY_MODE | 0xB0 | — | defined, never sent |
| DEVICE_SET_LATCH_MODE | 0xB2 | `n()` latch / `q()` unlatch | |
| GET_WIFI_SCAN_RESULTS … SET_MQTT_PASSWORD | 0x07–0x0F | — | **Hub-only**; defined in enum but the lock app never sends them |

**Challenge correction (vs §4):** the 3-byte "challenge" is **client-generated with `java.util.Random`** (`B5/a.java`), echoing the lock's last value back — a weak replay token, **not** a lock-issued cryptographic nonce. The lock does check the echo (`INCORRECT_CHALLENGE=21`), but it carries no cryptographic strength. (§4's earlier "lock-issued nonce" wording is superseded by this.)

---

## 10. FAILSAFE TRACK — true Glue-independence (hardware, unstarted)

Chosen goal: survive Glue shutting down the backend. Since **all software paths are exhausted** (§8), the only remaining technical route is the **nRF52 chip's SWD debug port**. Status: **not yet attempted** — this is the track to continue later.

**The premise:** if Glue left **`APPROTECT`** (readback protection) disabled on the shipped nRF52 — common on production devices — then physically attaching a debugger reads the **entire flash: firmware + any embedded keys**. APPROTECT state is only knowable from the chip itself (invisible to app/API/BLE), so this must be tested on hardware.

**Rough procedure (to flesh out later):**
1. Open the lock; identify the nRF52 module and its **SWD pads** (SWDIO, SWCLK, GND, VDD) — often test points or a small header.
2. Attach a probe: **J-Link**, ST-Link, or a **Raspberry Pi Pico as a CMSIS-DAP/SWD probe** (~$5–20).
3. Read protection state + dump: `nrfjprog --memrd` / `--readcode`, or OpenOCD. If APPROTECT is **off** → full flash dumps out → analyze in Ghidra (Cortex-M, nRF52 base addresses) for the verification routine and any embedded secret.
4. If APPROTECT is **on** → readout blocked; options narrow to glitch/fault-injection (hard) or accepting the cloud-dependent model.

**Secondary hardware angle (flagged, lower odds):** buttonless Secure DFU is enabled; *if* the bootloader accepts unsigned or downgraded images, a crafted DFU `.zip` could run owner code that self-dumps flash. Hinges on bootloader config only readable on-chip.

**Parallel software track (daily use, separate from the failsafe):** build an **ESP32 + ESPHome** component (C++: CRC-16/CCITT + FrameBuilder + GATT sequencing from §4/§9) exposing a `lock` entity to **Home Assistant**, with HA minting Cognito tokens and burning/refilling the 5-key pool via `/keys`. This ends reliance on the broken iOS app **today** and works as long as Glue's API is up — but it is **not** Glue-proof (see §8 key-cap). Serial to scan for: `GL04A.DLXXXX.XXXX`.

---

## 11. LOCK STATE REPORTING & the HA sensor design (decided)

**Question:** does the lock report locked/unlocked? **Answer: not usefully on this unit.** Three mechanisms exist; none gives reliable local state on a GL04:

1. **`LockIsLocked (28)` / `LockIsUnlocked (29)`** — *response codes only* (used solely in `LockByteResponseCode`). Returned as the status byte after an OPERATE command ("already in that state"). **Not pollable** — only a side effect of acting. Still useful: confirms whether a lock/unlock command actually took.
2. **Bolt position sensor** — the proper state source: `BOLT_POSITION_ENABLE (0x75)` then `BOLT_POSITION_GET_STATE (0x76)` → `BoltState`: **`RETRACTED (0xA0)` = unlocked, `PROTRUDED (0xA1)` = locked.** BUT this is gated in the app to **GL05 hardware**, and **this lock is GL04** (`serialNumber GL04A.DLXXXX.XXXX`, `Hardware.isGL04()`). The app never enables it here; live data shows `boltPosition.isEnabled=false`, `status="unknown"`. Whether GL04 *firmware* answers these opcodes anyway is unknown — **probe directly with `bleak` during the hardware session.**
3. **Backend `doorStatus`/`doorState`** — cloud-reported; `"hidden"`/`"unknown"` on this account.

**Hub clarification (owner has an unpaired hub; pairing failed per ticket #40265):** there *is* a hub-gated door-status feature in the app (`GetLockFeaturesActor.java:208`, flag `z9`) — but it **also requires `isGL05()`**, so it's false on a GL04 regardless of hub. Hub install sends **no** bolt-enable command to the lock. A working hub would only add **cloud** remote-operation + **cloud** door-status (MQTT→backend) — cloud-dependent, not a local BLE state read. So **a hub does not solve local state for an offline HA setup.** `LockFeatures` = `(notifications, z9[GL05+hub], doorOpenAlert, boltEnabled, doorStatusOptIn[GL05+no-hub])` — built in `picapau/features/lock/GetLockFeaturesActor.java`.

### Decision: owner supplies ground-truth state via own sensor; ESPHome component designed for BOTH
Since the lock won't self-report, state comes from owner-added sensing. Two approaches, to be supported by one component:

- **Option A — internal bolt sensor (always-true state).** Reed/Hall sensor + magnet on the moving bolt/cam inside the lock body → true `locked`/`unlocked` regardless of what moved the bolt (ESP32, key, thumbturn). Requires opening the lock → **do it in the same teardown as the nRF52 SWD dump (§10).** The best state source; the fiddly build (power/space inside a battery device).
- **Option B — external door reed + auto-act on close (no teardown, build first).** A door-frame reed switch reports open/closed; on close the ESP32 drives the lock to a known state. **Done right:** confirm via the **OPERATE response code** (`SUCCESS` vs `LockIsLocked`/`MotorNotReady`/`Overcurrent`) so state is *confirmed*, not assumed. Caveats: "door closed" ≠ "bolt thrown"; an open door must force `unlocked`/unavailable, never "locked"; **auto-act burns a key/API call per close** — mind the 5-key offline cap (rate-limit, or only auto-act when online).

**Chosen design:**
- **Close behavior = config-selectable** (HA/ESPHome setting: always-lock / always-unlock / sensor-only-no-auto-act).
- **Component architected for both sensors from the start** — door reed now; optional bolt/Hall GPIO input later is a config change, not a rewrite.
- **State precedence when both present:** internal bolt sensor (truth) > OPERATE response confirmation > door-reed-derived assumption. Door-open ⇒ report open/unavailable, suppress "locked".

---

## 12. ESPHome / Home Assistant integration plan (software track, daily use)

Goal: expose the lock as a native HA `lock` entity, cloud-independent for the *operation*, via an ESP32 near the door.

- **Why a custom C++ external component (not plain YAML):** the protocol is stateful/computed — CRC-16/CCITT per frame, the 3-byte challenge echo, 20-byte-chunked GATT writes, frame assembly `[cmd][len LE][challenge][payload][crc LE]`, and next-unused-key selection. Not expressible in declarative YAML. Port `FrameBuilder`/`BLEProtocol`/CRC from §4/§9 into an `esphome::Component` using `esp32_ble_client`.
- **BLE target:** scan name = **`GL04A.DLXXXX.XXXX`**; lock service `E4B264D7-4E56-424F-8B5C-D4089EA216EF`; write/notify on `LOCK_PROTOCOL`; wake via `WAKE_UP` char (raw `0x01`).
- **Unlock/lock sequence:** wake → `LOG_IN(0x19, offline key)` → `OPERATE(0x32, 0x00 unlock / 0x01 lock)`; read response status for state confirmation (§11).
- **Key supply (the hard constraint):** 5-key rolling pool, server-refilled on consume (§8). Design: **HA holds Cognito creds, mints a token, calls `GET /glue-osm-devices/{id}/keys`, pushes the bank to the ESP32**; ESP32 unlocks offline from the bank; a burn is reported so the server advances the window. Works while Glue's API lives; **not Glue-proof** (that's §10's hardware track).
- **State:** per §11 — owner sensor(s) + OPERATE response confirmation.
- **Validate first:** one throwaway `bleak` run of the unlock sequence against the physical lock before committing to C++ (also probe `BOLT_POSITION_*` to see if GL04 firmware answers).

---

### Open items for the continued track
- [ ] Teardown photos → locate nRF52 SWD pads on this hardware rev (hw 3).
- [ ] Read APPROTECT state (the go/no-go gate for everything downstream).
- [ ] If readable: dump flash, load in Ghidra, locate key-verification + any embedded key.
- [ ] While lock is open for SWD: fit the **internal bolt reed/Hall sensor** (Option A, §11) — one teardown, both jobs.
- [ ] Validate the §4/§9 unlock sequence once against the physical lock with a throwaway `bleak` script before committing to C++.
- [ ] `bleak` probe: does GL04 firmware answer `BOLT_POSITION_ENABLE`/`GET_STATE` despite the app's GL05 gate? (free real-state if yes.)
- [ ] Build ESPHome component (§12): door-reed + config-selectable close behavior first; optional bolt/Hall GPIO input; HA-driven key refresh.
- [ ] Decide credential route for key refresh: live Bearer capture vs. Cognito self-auth in HA (token TTL = 60 min).

---

## Key file reference (under `decompiled/sources/`)

| Concern | File |
|---|---|
| BLE frame + CRC | `gluehome/gluetooth/sdk/v2/internals/ble/BLEProtocol.java`, `.../ble/O.java` |
| Command builders / set-time | `.../v2/internals/FrameBuilder.java` (`p()`=login, `r()`=unlock, `o()`=lock, `D(epoch)`=set-time) |
| Opcodes / operate / responses | `.../ble/DeviceCommand.java`, `OperateCommand.java`, `LockByteResponseCode.java` |
| UUIDs | `.../ble/Characteristic.java`, `p173s5/a.java` |
| Keys / provisioning | `domain/features/lock/KeyRepository.java`, `w5/LockKey.java` |
| Key storage (unencrypted) | `.../sdk/database/GluetoothDatabase*.java`, `o5/AccessKeyEntity.java` |
| Remote op / hub | `v2/hub/models/*.java`, `picapau/data/features/locks/GlueLockRepositoryImpl.java`, `RemoteOperationsImpl.java` |
| REST services | `p082i5/*.java`; facade `Q7/b.java`; session `p073h5/a.java` |
| Auth | `picapau/data/features/auth/Cognito.java`, `interceptors/CognitoTokenProvider.java`, `SecureUserDetails.java` |
| Env/secrets bootstrap | `picapau/PicaPauApplication.java`, `p184t7/C2181o0.java` (Seguro pwd line 546), `decoded/res/values/strings.xml` |
| Auto-unlock | `picapau/features/autounlock/*`, `core/framework/geofencer/*`, `scanner/ScanReceiver.java` |

"""ESPHome external component: Glue smart lock over BLE.

Wraps the validated frame protocol (glue_protocol.h) and drives the lock as a
native Home Assistant `lock` entity. Auth = single-use offline keys handed to the
device by Home Assistant; the ESP32 holds no cloud credentials. See
ESPHOME_COMPONENT_PLAN.md and GLUE_APP_TECHNICAL_WRITEUP.md.

Depends on the built-in `ble_client`, which must be given the lock's MAC in YAML.
"""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import ble_client
from esphome.const import CONF_ID
import uuid

CODEOWNERS = ["@adde"]
DEPENDENCIES = ["ble_client", "esp32"]
AUTO_LOAD = ["lock"]
MULTI_CONF = True

glue_lock_ns = cg.esphome_ns.namespace("glue_lock")
GlueLockHub = glue_lock_ns.class_("GlueLockHub", cg.Component, ble_client.BLEClientNode)

CONF_GLUE_LOCK_ID = "glue_lock_id"
CONF_LOCK_ID = "lock_id"


def validate_lock_id(value):
    try:
        return str(uuid.UUID(cv.string(value)))
    except ValueError as err:
        raise cv.Invalid("lock_id must be the Glue lock UUID") from err

CONFIG_SCHEMA = (
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(GlueLockHub),
            cv.Required(CONF_LOCK_ID): validate_lock_id,
        }
    )
    .extend(ble_client.BLE_CLIENT_SCHEMA)
    .extend(cv.COMPONENT_SCHEMA)
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    cg.add(var.set_lock_id(config[CONF_LOCK_ID]))
    await cg.register_component(var, config)
    await ble_client.register_ble_node(var, config)

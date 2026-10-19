"""`lock` platform for the Glue lock, plus the HA->device key-push service."""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import lock
from esphome.const import CONF_ID
from . import glue_lock_ns, GlueLockHub, CONF_GLUE_LOCK_ID

DEPENDENCIES = ["glue_lock"]

GlueLock = glue_lock_ns.class_("GlueLock", lock.Lock, cg.Parented.template(GlueLockHub))

CONFIG_SCHEMA = lock.lock_schema(GlueLock).extend(
    {
        cv.GenerateID(CONF_GLUE_LOCK_ID): cv.use_id(GlueLockHub),
    }
)


async def to_code(config):
    var = await lock.new_lock(config)
    hub = await cg.get_variable(config[CONF_GLUE_LOCK_ID])
    await cg.register_parented(var, config[CONF_GLUE_LOCK_ID])
    cg.add(hub.set_lock_entity(var))

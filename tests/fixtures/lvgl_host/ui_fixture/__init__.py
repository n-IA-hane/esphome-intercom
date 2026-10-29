"""Host-only UI owner probes, without audio, networking or device hardware."""
import esphome.codegen as cg
from esphome import automation
from esphome.components import switch as switch_component
import esphome.config_validation as cv

DEPENDENCIES = ["host"]
ui_fixture_ns = cg.esphome_ns.namespace("ui_fixture")
UiPhone = ui_fixture_ns.class_("UiPhone")
UiRuntime = ui_fixture_ns.class_("UiRuntime")
UiVideo = ui_fixture_ns.class_("UiVideo")
CONFIG_SCHEMA = cv.Schema({cv.Optional("phone_id"): cv.declare_id(UiPhone),
                         cv.Optional("runtime_id"): cv.declare_id(UiRuntime),
                         cv.Optional("video_id"): cv.declare_id(UiVideo),
                         cv.Optional("conference_ring_id"): cv.use_id(switch_component.Switch)})

# These are backend boundaries of the imported phone pages, not a SIP model.
# Their production names and widget bindings stay unchanged in the fixture.
UiVoipAction = ui_fixture_ns.class_("UiVoipAction", automation.Action)
for name in ("voip_stack.call_toggle", "voip_stack.decline_call", "voip_stack.next_contact", "voip_stack.prev_contact"):
    @automation.register_action(name, UiVoipAction, cv.Any(None, cv.Schema({}, extra=cv.ALLOW_EXTRA)), synchronous=True)
    async def action_to_code(config, action_id, template_arg, args):
        return cg.new_Pvariable(action_id, template_arg)


async def to_code(config):
    if "phone_id" in config:
        phone = cg.new_Pvariable(config["phone_id"])
        if "conference_ring_id" in config:
            setting = await cg.get_variable(config["conference_ring_id"])
            cg.add(phone.set_conference_ring_switch(setting))
    if "runtime_id" in config:
        cg.new_Pvariable(config["runtime_id"])
    if "video_id" in config:
        cg.new_Pvariable(config["video_id"])


UiDialAction = ui_fixture_ns.class_("UiDialAction", automation.Action)


@automation.register_action("voip_stack.call", UiDialAction,
                            cv.Schema({cv.Required("target"): cv.templatable(cv.string)}), synchronous=True)
async def dial_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    cg.add(var.set_target(await cg.templatable(config["target"], args, cg.std_string)))
    return var

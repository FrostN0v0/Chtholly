from nonebot import require
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_alconna")
require("nonebot_plugin_uninfo")

__plugin_meta__ = PluginMetadata(
    name="Plugin Manager",
    description="Plugin-level runtime switch manager.",
    usage=(
        "/plugin list | /plugin status <plugin> | "
        "/plugin enable <plugin> | /plugin disable <plugin>"
    ),
    type="application",
)

from . import commands as commands
from . import guard as guard

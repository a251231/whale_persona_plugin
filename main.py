from __future__ import annotations
from langbot_plugin.api.definition.plugin import BasePlugin


class WhalePersonaPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__()

    async def initialize(self) -> None:
        pass

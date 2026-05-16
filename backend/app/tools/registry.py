"""
Tool registry: maps tool names to implementations and produces the JSON schema
list Ollama expects in the `tools` field of a chat request.
"""
from collections.abc import Callable, Awaitable
from typing import Any

ToolFn = Callable[..., Awaitable[Any]]


class Tool:
    __slots__ = ("name", "description", "parameters", "fn")

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        fn: ToolFn,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.fn = fn

    def to_ollama_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.to_ollama_schema() for t in self._tools.values()]

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ValueError(f"Unknown tool: {name!r}")
        return await tool.fn(**arguments)

"""Tool registry: each tool is a plain function plus the JSON schema Ollama sees."""

import inspect
import json
import traceback
from typing import Callable

TOOLS: dict[str, dict] = {}


def tool(description: str, **params: str):
    """Register a tool. Each keyword is a parameter: "type: description", with "?" after the type if optional."""

    def wrap(fn: Callable):
        props, required = {}, []
        for name, spec in params.items():
            kind, _, desc = spec.partition(":")
            kind = kind.strip()
            optional = kind.endswith("?")
            kind = kind.rstrip("?")
            props[name] = {"type": kind, "description": desc.strip()}
            if not optional:
                required.append(name)
        TOOLS[fn.__name__] = {
            "fn": fn,
            "schema": {"type": "function", "function": {
                "name": fn.__name__, "description": description,
                "parameters": {"type": "object", "properties": props, "required": required}}},
        }
        return fn

    return wrap


def schemas() -> list[dict]:
    return [t["schema"] for t in TOOLS.values()]


def run(name: str, args: dict | str | None) -> str:
    t = TOOLS.get(name)
    if not t:
        return f"Unknown tool {name}."
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except json.JSONDecodeError:
            args = {}
    args = args or {}
    accepted = inspect.signature(t["fn"]).parameters
    args = {k: v for k, v in args.items() if k in accepted}
    try:
        result = t["fn"](**args)
    except TypeError as e:
        return f"Bad arguments for {name}: {e}"
    except Exception as e:  # a tool failing must not end the conversation
        traceback.print_exc()
        return f"{name} failed: {e}"
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)


from . import info, system, memory, timers, network  # noqa: E402,F401  (registers the tools)

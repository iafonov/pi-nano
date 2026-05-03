#!/usr/bin/env python3
"""A tiny coding-agent CLI skeleton.

This first version implements the local conversation loop, local tools, and a
basic Ollama-backed LLM call.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_SYSTEM_PROMPT = """You are a coding agent running locally in a terminal.
You can inspect and modify files using tools.

Available tools:
- read: read a file
- write: write a file
- edit: replace exact text in a file
- bash: run a shell command

Guidelines:
- Inspect files before editing them.
- Prefer exact edits over rewriting whole files.
- Keep answers concise.
- Show file paths clearly.
- Do not claim a change was made unless a tool succeeded.
- Ask before destructive actions.
- If you need to inspect, create, edit, or run anything, call a tool.
- Do not say "I'll read/edit/run/fix" unless you call the appropriate tool in the same response.
"""


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] | None = None


Message = dict[str, Any]

GRAY = "\033[2;90m"
PUNK = "\033[1;35m"
RESET = "\033[0m"

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a local text file and return its contents.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Create or overwrite a local text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace exact text in a local text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]


def load_system_prompt() -> str:
    agents_path = Path("AGENTS.md")
    if not agents_path.exists():
        return BASE_SYSTEM_PROMPT

    agents_text = agents_path.read_text(encoding="utf-8")
    return f"{BASE_SYSTEM_PROMPT}\n\nRepo instructions from AGENTS.md:\n{agents_text}"


def create_history_file() -> Path:
    history_dir = Path("history")
    history_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return history_dir / f"session_{timestamp}.jsonl"


def write_trajectory(path: Path, event: dict[str, Any]) -> None:
    event = {"timestamp": datetime.now().isoformat(timespec="seconds"), **event}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def approximate_context_percent(messages: list[Message]) -> float:
    chars = len(json.dumps(messages))
    tokens = chars / 3.5
    return min(tokens / 262_144 * 100, 100)


def start_wait_dots(message: str) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()

    def run() -> None:
        print(f"{GRAY}{message}", end="", flush=True)
        while not stop.wait(1):
            print(".", end="", flush=True)
        print(RESET, flush=True)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return stop, thread


def read_tool(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")[:50_000]


def write_tool(path: str, content: str) -> str:
    Path(path).write_text(content, encoding="utf-8")
    return f"Wrote {path}"


def preview_text(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... truncated {len(text) - limit} chars"


def edit_tool(path: str, old_text: str, new_text: str) -> str:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")

    if old_text not in text:
        raise ValueError("old_text not found")

    file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return (
        f"Edited {path}\n"
        f"Replaced:\n--- old\n{preview_text(old_text)}\n"
        f"+++ new\n{preview_text(new_text)}"
    )


READ_ONLY_BASH_PREFIXES = (
    "ls",
    "pwd",
    "cat",
    "head",
    "tail",
    "grep",
    "rg",
    "find",
    "wc",
    "du",
    "df",
    "file",
    "stat",
    "git status",
    "git diff",
    "git log",
    "python3 -m py_compile",
    "hostname",
    "whoami",
    "date",
    "uname",
    "sysctl",
    "vm_stat",
    "ps",
    "which",
)


def should_auto_accept_bash(command: str) -> bool:
    stripped = command.strip()
    if not stripped:
        return False
    dangerous_tokens = (";", "&&", "||", "|", ">", "<", "`", "$(")
    if any(token in stripped for token in dangerous_tokens):
        return False
    return any(
        stripped == prefix or stripped.startswith(prefix + " ")
        for prefix in READ_ONLY_BASH_PREFIXES
    )


def bash_tool(command: str) -> str:
    result = subprocess.run(
        command,
        shell=True,
        text=True,
        capture_output=True,
        timeout=60,
    )
    return (result.stdout + result.stderr)[:50_000]


def run_tool(name: str, arguments: dict[str, Any]) -> str:
    try:
        if name == "read":
            return read_tool(arguments["path"])
        if name == "write":
            return write_tool(arguments["path"], arguments["content"])
        if name == "edit":
            return edit_tool(arguments["path"], arguments["old_text"], arguments["new_text"])
        if name == "bash":
            return bash_tool(arguments["command"])
        raise ValueError(f"Unknown tool: {name}")
    except Exception as exc:
        return f"Tool error: {exc}"


def parse_text_tool_calls(content: str) -> tuple[str, list[ToolCall]]:
    """Parse Qwen-style textual tool calls if Ollama did not return structured ones."""
    tool_calls: list[ToolCall] = []
    pattern = re.compile(r"<function=([a-zA-Z_][\w-]*)>(.*?)</function>", re.DOTALL)
    param_pattern = re.compile(
        r"<parameter=([a-zA-Z_][\w-]*)>\s*(.*?)\s*</parameter>",
        re.DOTALL,
    )

    for match in pattern.finditer(content):
        name = match.group(1)
        body = match.group(2)
        arguments = {
            param.group(1): param.group(2).strip()
            for param in param_pattern.finditer(body)
        }
        tool_calls.append(ToolCall(name=name, arguments=arguments))

    cleaned = pattern.sub("", content)
    cleaned = cleaned.replace("</tool_call>", "").strip()
    return cleaned, tool_calls


def ollama_message(message: Message) -> Message:
    formatted = {"role": message["role"], "content": message.get("content", "")}
    if message.get("tool_calls"):
        formatted["tool_calls"] = message["tool_calls"]
    return formatted


def call_llm(messages: list[Message], trajectory_path: Path, debug: bool) -> LLMResponse:
    """Call the local Ollama chat API."""
    if debug:
        print(f"{GRAY}--- LLM input ---")
        print(json.dumps(messages, indent=2))
        print(f"--- end LLM input ---{RESET}")

    payload = {
        "model": "qwen3-coder:latest",
        "messages": [
            ollama_message(message)
            for message in messages
            if message.get("role") in {"system", "user", "assistant", "tool"}
        ],
        "tools": TOOL_SCHEMAS,
        "stream": False,
    }

    request = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    context_percent = approximate_context_percent(messages)
    started_at = time.time()
    stop_dots, dots_thread = start_wait_dots(
        f"Waiting for LLM ({context_percent:.1f}% context used)"
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        write_trajectory(trajectory_path, {
            "type": "llm_call",
            "payload": payload,
            "error": str(exc),
            "duration_seconds": round(time.time() - started_at, 3),
        })
        return LLMResponse(text=f"Ollama error: {exc}", tool_calls=[])
    finally:
        stop_dots.set()
        dots_thread.join(timeout=1)

    write_trajectory(trajectory_path, {
        "type": "llm_call",
        "payload": payload,
        "response": data,
        "duration_seconds": round(time.time() - started_at, 3),
    })

    message = data.get("message", {})
    content = message.get("content", "")
    tool_calls = []

    for call in message.get("tool_calls", []) or []:
        function = call.get("function", {})
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        tool_calls.append(ToolCall(name=function.get("name", ""), arguments=arguments))

    if not tool_calls:
        content, tool_calls = parse_text_tool_calls(content)

    return LLMResponse(text=content, tool_calls=tool_calls)


def looks_like_pending_tool_action(text: str) -> bool:
    lowered = text.lower()
    action_phrases = (
        "i'll read",
        "i will read",
        "let me read",
        "i'll check",
        "let me check",
        "i'll inspect",
        "let me inspect",
        "i'll create",
        "i will create",
        "i'll write",
        "i will write",
        "i'll edit",
        "i will edit",
        "let me edit",
        "i'll fix",
        "i will fix",
        "let me fix",
        "i'll run",
        "i will run",
        "let me run",
    )
    return any(phrase in lowered for phrase in action_phrases)


def run_agent_turn(messages: list[Message], trajectory_path: Path, debug: bool) -> None:
    nudge_used = False

    while True:
        response = call_llm(messages, trajectory_path, debug)
        assistant_message: Message = {"role": "assistant", "content": response.text}
        if response.tool_calls:
            assistant_message["tool_calls"] = [
                {"function": {"name": call.name, "arguments": call.arguments}}
                for call in response.tool_calls
            ]
        messages.append(assistant_message)

        if response.text:
            print(response.text)

        if not response.tool_calls:
            if not nudge_used and looks_like_pending_tool_action(response.text):
                nudge_used = True
                messages.append({
                    "role": "user",
                    "content": (
                        "You said you would perform an action, but you did not call a tool. "
                        "Call the appropriate tool now, or explain why no tool is needed."
                    ),
                })
                continue
            return

        nudge_used = False

        for call in response.tool_calls:
            if call.name == "bash":
                command = call.arguments.get("command", "")
                print(f"{PUNK}[tool:bash]{RESET} $ {command}")
                if not should_auto_accept_bash(command):
                    try:
                        input("Press Enter to run, or Ctrl+C to cancel...")
                    except KeyboardInterrupt:
                        print("\nBash command cancelled.")
                        messages.append({
                            "role": "tool",
                            "name": call.name,
                            "content": "Tool cancelled by user.",
                        })
                        return
            elif call.name == "edit":
                print(
                    f"{PUNK}[tool:edit]{RESET}{GRAY} {call.arguments.get('path', '')}\n"
                    f"--- old\n{preview_text(call.arguments.get('old_text', ''))}\n"
                    f"+++ new\n{preview_text(call.arguments.get('new_text', ''))}{RESET}"
                )
            elif call.name == "read":
                path = Path(call.arguments.get("path", ""))
                size = path.stat().st_size if path.exists() else 0
                tokens = int(size / 3.5)
                print(f"{PUNK}[tool:read]{RESET} {path.resolve()} ({size} bytes, ~{tokens} tokens)")
            elif call.name == "write":
                path = Path(call.arguments.get("path", ""))
                content = call.arguments.get("content", "")
                size = len(content.encode("utf-8"))
                tokens = int(size / 3.5)
                print(f"{PUNK}[tool:write]{RESET} {path.resolve()} ({size} bytes, ~{tokens} tokens)")
                print(f"{GRAY}{content}{RESET}")
            else:
                print(f"{PUNK}[tool:{call.name}]{RESET}{GRAY} {json.dumps(call.arguments)}{RESET}")
            output = run_tool(call.name, call.arguments)
            messages.append({
                "role": "tool",
                "name": call.name,
                "content": output,
            })
            if call.name not in {"read", "write"} or debug:
                print(f"{PUNK}[tool:{call.name}]{RESET}{GRAY} {output}{RESET}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Tiny local coding agent")
    parser.add_argument("--debug", type=int, default=0, help="Show full LLM inputs when set to 1")
    args = parser.parse_args()
    debug = bool(args.debug)

    # Check that Ollama is running
    try:
        import urllib.request
        import json
        request = urllib.request.Request(
            "http://localhost:11434/api/tags",
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            model_name = "qwen3-coder:latest"  # Default model name
            if "models" in data:
                # Find the model we're using
                for model in data["models"]:
                    if model["name"] == "qwen3-coder:latest":
                        model_name = model["name"]
                        break
            print(f"Local Ollama API live and serving model: {model_name}")
    except Exception as exc:
        print(f"Warning: Could not connect to Ollama API: {exc}")
        print("Make sure Ollama is running with 'ollama serve'")
        return

    trajectory_path = create_history_file()
    print(f"History: {trajectory_path}")

    system_prompt = load_system_prompt()
    messages: list[Message] = [{"role": "system", "content": system_prompt}]

    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if text in {"/quit", "/exit", "quit", "exit"}:
            break
        if text == "/inspect":
            print(json.dumps(messages, indent=2))
            continue
        if text == "/reset":
            system_prompt = load_system_prompt()
            messages = [{"role": "system", "content": system_prompt}]
            print("Conversation reset.")
            continue
        if not text:
            continue

        messages.append({"role": "user", "content": text})
        run_agent_turn(messages, trajectory_path, debug)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Simple live integration harness for agent.py.

This uses the real local Ollama server/model. It runs the agent in a temporary
workspace, asks it to create/edit/run a hello-world Python file, and verifies the
observable results.
"""

from __future__ import annotations

import json
import re
import time
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
AGENT = ROOT / "agent.py"
AGENTS = ROOT / "AGENTS.md"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def fail(message: str, output: str = "") -> None:
    print(f"FAIL: {message}")
    if output:
        print("\n--- agent output ---")
        print(output)
        print("--- end agent output ---")
    raise SystemExit(1)


def assert_contains(haystack: str, needle: str, label: str) -> None:
    if needle not in haystack:
        fail(f"Missing {label!r}: expected to find {needle!r}", haystack)


def log(message: str) -> None:
    print(f"[harness] {message}", flush=True)


def run_agent(workdir: Path) -> str:
    prompt_lines = [
        'Create hello_world.py. You must use the write tool. The file content must be exactly: print("hello world")',
        "Run hello_world.py. You must use the bash tool to run exactly: python3 hello_world.py",
        "Edit hello_world.py. You must use the edit tool. Replace exactly hello world with hello worrrrlllldd.",
        "Run hello_world.py again. You must use the bash tool to run exactly: python3 hello_world.py",
        "Use the bash tool to run exactly: hostname. Then report the hostname output.",
        "Fetch https://example.com using the internet tool. Then use bash to extract the page title from the saved file into internet_title.txt. Do not summarize the page.",
        "/rewind 3",
        "Use the bash tool to run exactly: pwd",
        "/exit",
    ]
    input_lines = [
        prompt_lines[0],
        prompt_lines[1],
        "",  # confirm python3 hello_world.py bash command
        prompt_lines[2],
        prompt_lines[3],
        "",  # confirm python3 hello_world.py bash command
        prompt_lines[4],
        prompt_lines[5],
        "",  # approve possible extraction bash command
        "",  # approve possible verification bash command
        "",  # approve possible retry bash command
        "",  # approve possible verification bash command
        "",  # extra approval if the model retries
        prompt_lines[6],
        prompt_lines[7],
        prompt_lines[8],
    ]
    prompts = "\n".join([*input_lines, ""])

    log("planned prompts:")
    for index, prompt in enumerate(prompt_lines, 1):
        log(f"  {index}. {prompt}")
    log("stdin script includes blank lines to approve interactive bash confirmations")

    log(f"starting agent.py in {workdir}")
    started_at = time.time()
    proc = subprocess.Popen(
        [sys.executable, "agent.py"],
        cwd=workdir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        output, _ = proc.communicate(prompts, timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        output, _ = proc.communicate()
        fail("agent.py timed out", strip_ansi(output))

    elapsed = time.time() - started_at
    log(f"agent.py exited with code {proc.returncode} after {elapsed:.1f}s")

    output = strip_ansi(output)
    log(f"captured {len(output)} chars of agent output")
    print("\n--- agent output ---")
    print(output)
    print("--- end agent output ---\n")

    if proc.returncode != 0:
        fail(f"agent.py exited with code {proc.returncode}", output)

    return output


def verify_history(workdir: Path) -> None:
    log("verifying history JSONL")
    history_files = sorted((workdir / "history").glob("session_*.jsonl"))
    if not history_files:
        fail("no history/session_*.jsonl file was created")

    log(f"found history file: {history_files[-1]}")
    lines = history_files[-1].read_text(encoding="utf-8").splitlines()
    log(f"history contains {len(lines)} JSONL events")
    if not lines:
        fail(f"history file is empty: {history_files[-1]}")

    for line in lines:
        event = json.loads(line)
        if event.get("type") == "llm_call":
            log("history contains at least one llm_call event")
            return

    fail(f"history file has no llm_call events: {history_files[-1]}")


def check_internet_prereq() -> None:
    log("checking internet prerequisite with https://example.com")
    try:
        with urllib.request.urlopen("https://example.com", timeout=20) as response:
            log(f"example.com reachable with status {response.status}")
    except Exception as exc:
        fail(f"internet prerequisite failed: {exc}")


def main() -> None:
    log("starting live agent harness")
    if not AGENT.exists():
        fail(f"missing {AGENT}")

    check_internet_prereq()

    with tempfile.TemporaryDirectory(prefix="agent_live_test_") as tmp:
        workdir = Path(tmp)
        log(f"created temp workspace: {workdir}")
        shutil.copy2(AGENT, workdir / "agent.py")
        log("copied agent.py")
        if AGENTS.exists():
            shutil.copy2(AGENTS, workdir / "AGENTS.md")
            log("copied AGENTS.md")

        output = run_agent(workdir)

        log("verifying hello_world.py was created")
        hello_path = workdir / "hello_world.py"
        if not hello_path.exists():
            fail("hello_world.py was not created", output)

        content = hello_path.read_text(encoding="utf-8")
        log(f"hello_world.py content: {content!r}")
        if "hello worrrrlllldd" not in content:
            fail("hello_world.py was not edited to contain hello worrrrlllldd", output)

        log("running hello_world.py directly for final verification")
        run = subprocess.run(
            [sys.executable, str(hello_path)],
            text=True,
            capture_output=True,
            timeout=10,
        )
        if run.returncode != 0:
            fail(f"hello_world.py failed when run directly: {run.stderr}", output)
        if run.stdout.strip() != "hello worrrrlllldd":
            fail(f"unexpected direct hello_world.py output: {run.stdout!r}", output)

        log(f"direct hello_world.py stdout: {run.stdout!r}")

        expected_hostname = socket.gethostname().split(".")[0]
        log(f"expected hostname marker: {expected_hostname}")
        log("checking captured output markers")
        assert_contains(output, "[tool:write]", "write tool output")
        assert_contains(output, "[tool:edit]", "edit tool output")
        assert_contains(
            output, "[tool:bash] $ python3 hello_world.py", "python bash command"
        )
        assert_contains(output, "hello world", "initial hello-world output")
        assert_contains(output, "hello worrrrlllldd", "edited hello-world output")
        assert_contains(output, "[tool:bash] $ hostname", "hostname bash command")
        assert_contains(output, expected_hostname, "hostname output")
        assert_contains(
            output, "[tool:internet] https://example.com", "internet tool call"
        )
        assert_contains(
            output, "Saved to: /tmp/pi_nano_example.com_", "internet saved-path output"
        )
        assert_contains(output, "Rewound to turn 3.", "rewind confirmation")
        assert_contains(output, "[tool:bash] $ pwd", "pwd bash command after rewind")

        log("verifying internet_title.txt was created before rewind")
        title_path = workdir / "internet_title.txt"
        if not title_path.exists():
            fail("internet_title.txt was not created", output)
        title = title_path.read_text(encoding="utf-8").strip()
        log(f"internet_title.txt content: {title!r}")
        if "Example Domain" not in title:
            fail("internet_title.txt does not contain Example Domain", output)

        verify_history(workdir)

    print("PASS: live agent harness completed successfully")


if __name__ == "__main__":
    main()

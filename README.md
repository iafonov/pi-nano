# pi-nano

A tiny Python coding-agent experiment powered by local Ollama.

The whole agent lives in `agent.py`. It keeps a conversation history, sends it to Ollama, executes local tools requested by the model, and feeds tool results back into the next LLM call.

<img width="502" height="370" alt="Agent" src="https://github.com/user-attachments/assets/75e3351d-1b24-4fff-879a-7448701c1732" />

## Features

- Tools: `read`, `write`, `edit`, `bash`, `internet`
- Bash confirmation with a small read-only auto-allow list
- `AGENTS.md` repo instructions injected into the system prompt
- JSONL history logs in `history/session_<timestamp>.jsonl`
- Live integration test harness

## Run

Start Ollama:

```bash
ollama serve
```

Make sure the model exists:

```bash
ollama pull qwen3-coder:latest
```

Run the agent:

```bash
python3 agent.py
```

Debug mode prints full LLM inputs:

```bash
python3 agent.py --debug 1
```

Useful commands inside the agent:

- `/inspect` — print current message chain
- `/rewind N` — rewind conversation state back to turn `N`
- `/reset` — reset conversation and reload `AGENTS.md`
- `/exit` — quit

## Test

```bash
python3 test_harness.py
```

The harness uses the live local Ollama server, creates a temporary workspace, asks the agent to create/edit/run a hello-world script, checks `bash`, tests `internet`, tests `/rewind`, and verifies history logging.

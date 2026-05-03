# pi-nano

A tiny Python coding-agent experiment powered by local Ollama and `qwen3-coder:latest`.

The whole agent lives in `agent.py`. It keeps a conversation history, sends it to Ollama, executes local tools requested by the model, and feeds tool results back into the next LLM call.

## Features

- Local file tools: `read`, `write`, `edit`
- Shell tool: `bash`
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
- `/reset` — reset conversation and reload `AGENTS.md`
- `/exit` — quit

## Test

```bash
python3 test_harness.py
```

The harness uses the live local Ollama server, creates a temporary workspace, asks the agent to create/edit/run a hello-world script, checks `bash`, and verifies history logging.

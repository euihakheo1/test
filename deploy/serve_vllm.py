"""Launch an independently installed vLLM server; keep its key out of process arguments.

vLLM has its own CUDA/PyTorch environment, separate from the API's frozen dependencies.
Default: Linux/WSL, var/vllm-env/bin/vllm, Qwen3 text model, localhost:8001.
"""

from __future__ import annotations

import os
from pathlib import Path

from jettae.config import load_env
from jettae.llm.vllm import DEFAULT_MODEL


def main() -> None:
    load_env()
    binary = Path(os.environ.get("JETTAE_VLLM_BIN") or "var/vllm-env/bin/vllm").resolve()
    if not binary.is_file():
        raise SystemExit("Install the separate vLLM environment first: docs/GETTING_STARTED.md")
    key = os.environ.get("JETTAE_VLLM_API_KEY")
    if not key:
        raise SystemExit("Select .env.vllm with JETTAE_ENV_FILE before starting inference")
    model = os.environ.get("JETTAE_VLLM_WEIGHTS") or "Qwen/Qwen3-4B-Instruct-2507"
    argv = [
        str(binary),
        "serve",
        model,
        "--served-model-name",
        os.environ.get("JETTAE_VLLM_MODEL") or DEFAULT_MODEL,
        "--host",
        "127.0.0.1",
        "--port",
        "8001",
        "--dtype",
        "half",
        "--max-model-len",
        os.environ.get("JETTAE_VLLM_CONTEXT") or "16384",
        "--max-num-seqs",
        "2",
        "--gpu-memory-utilization",
        "0.85",
        "--enforce-eager",
        "--generation-config",
        "vllm",
        "--disable-log-requests",
    ]
    if revision := os.environ.get("JETTAE_VLLM_REVISION"):
        argv.extend(("--revision", revision))
    env = dict(os.environ, VLLM_API_KEY=key)
    # No shell or argv key; unrelated cloud credentials are not inherited by inference.
    for name in tuple(env):
        if name.startswith(("OPENAI_", "ANTHROPIC_")):
            del env[name]
    os.execve(binary, argv, env)


if __name__ == "__main__":
    main()

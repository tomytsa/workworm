"""Structured LLM calls through the local Claude Code CLI (uses the user's subscription)."""

from __future__ import annotations

import json
import shutil
import subprocess


class LLMError(RuntimeError):
    pass


def ask(system: str, prompt: str, schema: dict, model: str = "sonnet", timeout: int = 240,
        extra: list[str] | None = None) -> dict:
    """Run `claude -p` (tool-less unless `extra` adds MCP tools) and return its JSON-schema-validated output."""
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("No se encontró el CLI `claude` en el PATH")
    cmd = [exe, "-p", "--model", model, "--output-format", "json", "--tools", "",
           "--no-session-persistence", "--json-schema", json.dumps(schema), "--system-prompt", system, *(extra or [])]
    try:
        done = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise LLMError("Claude no respondió a tiempo") from exc
    try:
        result = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Salida inesperada de Claude: {(done.stdout or done.stderr)[:300]}") from exc
    if result.get("is_error") or not isinstance(result.get("structured_output"), dict):
        raise LLMError(f"Claude devolvió un error: {str(result.get('result') or result)[:300]}")
    return result["structured_output"]

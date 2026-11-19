"""
codex-bridge — a tiny, locked-down HTTP API that lets a trusted remote client
(for example your Instinct assistant) send a prompt to this machine, have it
run through the Codex CLI, and get the output back.

Security model
--------------
* Every endpoint requires `Authorization: Bearer <token>` (token lives in
  config.json, which is git-ignored and never committed).
* Only commands named in `allowed_commands` can run. Each entry maps to a
  fixed argv prefix; the client's prompt is appended as ONE extra argv
  element. Nothing ever goes through a shell, so there is no open-shell
  access and no way to run arbitrary commands.
* The server binds to 127.0.0.1 by default. Expose it through a tunnel
  (cloudflared or ngrok) which provides the HTTPS layer — see README.md.
"""

import hmac
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

CONFIG_PATH = Path(__file__).with_name("config.json")
log = logging.getLogger("codex-bridge")


def load_config() -> dict:
    """Load config.json and refuse to start with a missing or placeholder token."""
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Missing {CONFIG_PATH}.\n"
            "Copy config.example.json to config.json and set your token first."
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    token = cfg.get("token", "")
    if not token or token == "REPLACE_ME" or len(token) < 32:
        raise SystemExit(
            "Set a strong random token (at least 32 chars) in config.json — "
            "see README.md for the one-liner that generates one."
        )
    return cfg


CONFIG = load_config()

# Docs are disabled: no reason to advertise the API shape on a public tunnel URL.
app = FastAPI(title="codex-bridge", docs_url=None, redoc_url=None, openapi_url=None)


async def require_token(authorization: str = Header(default="")) -> None:
    """Dependency that enforces the bearer token on every endpoint.

    compare_digest avoids leaking the token through response timing.
    """
    expected = f"Bearer {CONFIG['token']}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid or missing token")


class RunRequest(BaseModel):
    command: str = Field(
        default="codex",
        description="Key into allowed_commands in config.json (default: 'codex')",
    )
    prompt: str = Field(
        min_length=1,
        description="The prompt. Passed to the CLI as a single argument, never through a shell.",
    )


@app.get("/health", dependencies=[Depends(require_token)])
async def health() -> dict:
    """Liveness check, also token-protected so the tunnel reveals nothing anonymously."""
    return {"status": "ok"}


@app.post("/run", dependencies=[Depends(require_token)])
async def run(req: RunRequest) -> dict:
    """Run an allowlisted command with the given prompt and return its output."""
    spec = CONFIG.get("allowed_commands", {}).get(req.command)
    if spec is None:
        raise HTTPException(
            status_code=403,
            detail=f"command '{req.command}' is not in the allowlist",
        )

    # The allowlist entry owns the argv prefix; the prompt is one extra element.
    # shell is never invoked, so metacharacters in the prompt cannot escape.
    argv = list(spec["argv"]) + [req.prompt]

    if shutil.which(argv[0]) is None:
        raise HTTPException(
            status_code=500,
            detail=f"'{argv[0]}' is not installed or not on PATH on this machine",
        )

    timeout = int(spec.get("timeout_seconds", CONFIG.get("default_timeout_seconds", 600)))
    started = time.monotonic()
    log.info("running command=%s (timeout=%ss)", req.command, timeout)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=CONFIG.get("workdir") or None,  # optional fixed working directory
            shell=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail=f"command timed out after {timeout}s",
        ) from None

    return {
        "command": req.command,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "duration_seconds": round(time.monotonic() - started, 2),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(
        app,
        host=CONFIG.get("host", "127.0.0.1"),  # localhost only; the tunnel exposes it
        port=int(CONFIG.get("port", 8787)),
    )

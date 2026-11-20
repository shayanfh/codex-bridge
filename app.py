"""
codex-bridge — a tiny, locked-down HTTP API that lets a trusted remote client
(for example your Instinct assistant) send a prompt to this machine, have it
run through the Codex CLI, and get the output back.

Security model
--------------
* Every endpoint requires `Authorization: Bearer <token>` (token lives in
  config.json, which is git-ignored and never committed).
* Only commands named in `allowed_commands` can run. Each entry maps to a
  fixed argv prefix that is executed exactly as written — the client's prompt
  is NEVER added to the argument list. It is delivered to the process on
  stdin, so a prompt can never be parsed as a CLI option, no matter what it
  starts with. Nothing ever goes through a shell.
* Any allowlisted entry that runs `codex` MUST include an explicit
  `--sandbox read-only` — the server refuses to start without it.
* Every run happens in a fixed, dedicated working directory (created next to
  this file by default) with a scrubbed minimal environment: only HOME, PATH,
  LANG/LC_ALL and TMPDIR, plus any variable names you explicitly list in
  `env_passthrough`. API keys and other secrets in your shell environment do
  NOT leak into the process unless you opt them in.
* Output and runtime are capped: stdout/stderr are truncated at
  `output_limit_chars` and the process is killed at the configured timeout.

The server binds to 127.0.0.1 by default. Think carefully before exposing it
through a tunnel — see the warning in README.md.
"""

import hmac
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

CONFIG_PATH = Path(__file__).with_name("config.json")
# Dedicated scratch directory for every run when config.workdir is null.
DEFAULT_WORKDIR = Path(__file__).parent / "bridge-workspace"
log = logging.getLogger("codex-bridge")


def _argv_forces_read_only_sandbox(argv: list) -> bool:
    """True if argv pins Codex to a read-only sandbox (both spellings)."""
    for i, arg in enumerate(argv):
        if arg in ("--sandbox", "-s") and i + 1 < len(argv) and argv[i + 1] == "read-only":
            return True
        if arg in ("--sandbox=read-only", "-s=read-only"):
            return True
    return False


def load_config() -> dict:
    """Load config.json and validate every safety invariant before serving."""
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

    commands = cfg.get("allowed_commands") or {}
    if not isinstance(commands, dict) or not commands:
        raise SystemExit("config.json must define at least one allowed_commands entry.")
    for name, spec in commands.items():
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise SystemExit(f"allowed_commands['{name}'].argv must be a non-empty list of strings.")
        # Hard rule: any entry that runs codex must pin a read-only sandbox.
        if Path(argv[0]).name == "codex" and not _argv_forces_read_only_sandbox(argv):
            raise SystemExit(
                f"allowed_commands['{name}'] runs codex without an explicit "
                "'--sandbox read-only'. Refusing to start — add it to the argv list."
            )
    return cfg


CONFIG = load_config()

# Fixed, dedicated working directory for every run — never the caller's choice,
# never the server's launch directory.
WORKDIR = Path(CONFIG.get("workdir") or DEFAULT_WORKDIR).expanduser().resolve()
WORKDIR.mkdir(parents=True, exist_ok=True)

# Scrubbed base environment: just enough for the CLI to find its binary, its
# config under HOME, and a temp dir. Everything else from the launch
# environment (API keys, tokens, proxies) is dropped unless explicitly opted
# in via env_passthrough in config.json.
BASE_ENV = {
    "HOME": str(Path.home()),
    "PATH": os.environ.get("PATH", os.defpath),
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TMPDIR": tempfile.gettempdir(),
}


def run_environment() -> dict:
    """Base env plus only the explicitly allowlisted passthrough variables."""
    env = dict(BASE_ENV)
    for name in CONFIG.get("env_passthrough", []):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def capped(text: str) -> tuple:
    """Truncate captured output to the configured limit. Returns (text, truncated)."""
    limit = int(CONFIG.get("output_limit_chars", 64000))
    if len(text) > limit:
        return text[:limit], True
    return text, False


# Docs are disabled: no reason to advertise the API shape on a shared URL.
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
        description="The prompt. Delivered to the process on stdin — never appended to argv.",
    )


@app.get("/health", dependencies=[Depends(require_token)])
async def health() -> dict:
    """Liveness check, also token-protected so the listener reveals nothing anonymously."""
    return {"status": "ok"}


@app.post("/run", dependencies=[Depends(require_token)])
async def run(req: RunRequest) -> dict:
    """Run an allowlisted command, feed the prompt on stdin, return capped output."""
    spec = CONFIG.get("allowed_commands", {}).get(req.command)
    if spec is None:
        raise HTTPException(
            status_code=403,
            detail=f"command '{req.command}' is not in the allowlist",
        )

    max_prompt = int(CONFIG.get("max_prompt_chars", 16000))
    if len(req.prompt) > max_prompt:
        raise HTTPException(
            status_code=413,
            detail=f"prompt is {len(req.prompt)} chars; limit is {max_prompt}",
        )

    # The argv is EXACTLY the allowlisted prefix — nothing from the request is
    # appended, so option-injection through the prompt is impossible by
    # construction. The prompt travels on stdin instead.
    argv = list(spec["argv"])

    if shutil.which(argv[0]) is None:
        raise HTTPException(
            status_code=500,
            detail=f"'{argv[0]}' is not installed or not on PATH on this machine",
        )

    # Timeout: per-command override, clamped to the configured hard ceiling.
    default_timeout = int(CONFIG.get("default_timeout_seconds", 300))
    max_timeout = int(CONFIG.get("max_timeout_seconds", 900))
    timeout = min(int(spec.get("timeout_seconds", default_timeout)), max_timeout)

    started = time.monotonic()
    log.info("running command=%s (timeout=%ss, cwd=%s)", req.command, timeout, WORKDIR)
    try:
        proc = subprocess.run(
            argv,
            input=req.prompt,          # prompt on stdin, never in argv
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(WORKDIR),          # fixed dedicated working directory
            env=run_environment(),     # scrubbed minimal environment
            shell=False,               # no shell, ever
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail=f"command timed out after {timeout}s",
        ) from None

    stdout, stdout_truncated = capped(proc.stdout)
    stderr, stderr_truncated = capped(proc.stderr)
    return {
        "command": req.command,
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "duration_seconds": round(time.monotonic() - started, 2),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(
        app,
        host=CONFIG.get("host", "127.0.0.1"),  # localhost only; see README before exposing
        port=int(CONFIG.get("port", 8787)),
    )

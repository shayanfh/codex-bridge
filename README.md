# codex-bridge

A tiny, locked-down local API that lets a trusted remote client (for example an
online assistant) send a prompt to your machine, run it through the
[Codex CLI](https://github.com/openai/codex), and get the output back.

```
remote client ──HTTPS──▶ tunnel (cloudflared/ngrok) ──▶ codex-bridge ──▶ codex exec --sandbox read-only   (prompt on stdin)
                  bearer token required on every request        localhost only, no shell, scrubbed env
```

> **⚠️ Do not expose this publicly until you have reviewed what it does.**
> Whoever can reach the server with the token can run Codex on your machine.
> Read the security model below, keep the bind address on `127.0.0.1`, and
> treat the tunnel URL + token as full access to this computer. Prefer a
> tunnel you can take down quickly, and take it down when you're not using it.

## Security model

- **Bearer token on every endpoint.** No token, no response — not even `/health`.
- **Command allowlist, no open shell.** Only commands you list in `config.json`
  can run. The argv is executed exactly as written — the prompt is **never
  appended to the argument list**. It is delivered to the process on **stdin**,
  so a prompt starting with `--` (or containing anything else) can never be
  parsed as a CLI option. Nothing ever goes through a shell.
- **Read-only sandbox is mandatory.** Any allowlisted entry that runs `codex`
  must include an explicit `--sandbox read-only`. The server refuses to start
  without it, so Codex cannot modify files outside what the sandbox allows.
- **Fixed, dedicated working directory.** Every run executes in `workdir`
  (default: a `bridge-workspace/` folder created next to `app.py`) — never in
  the caller's or the server's launch directory.
- **Scrubbed environment.** Runs get only `HOME`, `PATH`, `LANG`/`LC_ALL` and
  `TMPDIR`. API keys and other secrets in your shell environment are **not**
  passed through unless you name them in `env_passthrough`.
- **Capped output and runtime.** stdout/stderr are truncated at
  `output_limit_chars` (with a `truncated` flag in the response) and the
  process is killed at the timeout (`default_timeout_seconds`, clamped to
  `max_timeout_seconds`). Prompts over `max_prompt_chars` are rejected.
- **Your real config is git-ignored.** `config.json` (which holds the token)
  is never committed.

## Prerequisites

1. **Python 3.10+**
2. **Codex CLI installed and signed in:**
   ```bash
   npm install -g @openai/codex
   codex login
   ```
   The bridge runs `codex exec --sandbox read-only` (non-interactive mode) and
   pipes your prompt to it on stdin, which Codex reads as the task.
   If your Codex setup authenticates through an environment variable instead
   of `codex login` (for example `OPENAI_API_KEY`), add that variable name to
   `env_passthrough` in `config.json` — nothing is forwarded by default.

## Setup

```bash
git clone https://github.com/shayanfh/codex-bridge.git
cd codex-bridge

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp config.example.json config.json
```

Generate a strong token and put it in `config.json`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Run it

```bash
python app.py
```

It listens on `http://127.0.0.1:8787` (change host/port in `config.json`).

## Expose it (HTTPS via tunnel) — only after reviewing the warning above

Pick one:

```bash
# Option A: cloudflared quick tunnel (no account needed)
cloudflared tunnel --url http://localhost:8787

# Option B: ngrok
ngrok http 8787
```

Both print a public `https://…` URL. That URL plus your token is what you hand
to the remote client. Keep both private — anyone with them can run Codex on
your machine. Shut the tunnel down when you're done.

## API

All requests need the header: `Authorization: Bearer <your-token>`

### `GET /health`
```bash
curl -H "Authorization: Bearer $TOKEN" https://your-tunnel-url/health
# {"status":"ok"}
```

### `POST /run`
```bash
curl -X POST https://your-tunnel-url/run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "explain what this workspace contains"}'
```

Response:
```json
{
  "command": "codex",
  "exit_code": 0,
  "stdout": "…final Codex answer…",
  "stderr": "…progress log…",
  "stdout_truncated": false,
  "stderr_truncated": false,
  "duration_seconds": 12.34
}
```

## Configuration (`config.json`)

| Key | Meaning |
| --- | --- |
| `token` | Shared secret. Required, min 32 chars. |
| `host` / `port` | Bind address. Keep `127.0.0.1`. |
| `default_timeout_seconds` | Fallback per-run timeout (default 300). |
| `max_timeout_seconds` | Hard ceiling every timeout is clamped to (default 900). |
| `output_limit_chars` | stdout/stderr are truncated to this many chars each (default 64000). |
| `max_prompt_chars` | Prompts longer than this are rejected (default 16000). |
| `workdir` | Fixed working directory for every run. `null` = a `bridge-workspace/` folder next to `app.py`, created automatically. |
| `env_passthrough` | Names of environment variables allowed to reach the process. Empty = scrubbed minimal env only. |
| `allowed_commands` | The only runnable commands. Each is a fixed argv executed exactly as written; the prompt goes on stdin. |

Want different Codex flags? Edit the argv prefix — but any `codex` entry must
keep `--sandbox read-only` or the server will not start:

```json
"codex": { "argv": ["codex", "exec", "--sandbox", "read-only"], "timeout_seconds": 600 }
```

## Notes

- `codex exec` prints progress to `stderr` and the final answer to `stdout` —
  the API returns both, separately, each with a `truncated` flag.
- Long tasks: raise `timeout_seconds` in the allowlist entry (up to
  `max_timeout_seconds`) rather than retrying.
- Rotate the token any time by editing `config.json` and restarting.

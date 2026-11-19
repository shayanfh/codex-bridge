# codex-bridge

A tiny, locked-down local API that lets a trusted remote client (for example an
online assistant) send a prompt to your machine, run it through the
[Codex CLI](https://github.com/openai/codex), and get the output back.

```
remote client ──HTTPS──▶ tunnel (cloudflared/ngrok) ──▶ codex-bridge ──▶ codex exec "<prompt>"
                  bearer token required on every request        localhost only, no shell
```

## Security model

- **Bearer token on every endpoint.** No token, no response — not even `/health`.
- **Command allowlist, no open shell.** Only commands you list in
  `config.json` can run. Your prompt is passed to the CLI as a single argument;
  nothing is ever executed through a shell.
- **Localhost only.** The server binds to `127.0.0.1`. The tunnel is the only
  public surface, and it still requires the token.
- **Your real config is git-ignored.** `config.json` (which holds the token)
  is never committed.

## Prerequisites

1. **Python 3.10+**
2. **Codex CLI installed and signed in:**
   ```bash
   npm install -g @openai/codex
   codex login
   ```
   The bridge runs `codex exec "<prompt>"` (non-interactive mode), which uses
   the CLI's existing sign-in.

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

## Expose it (HTTPS via tunnel)

Pick one:

```bash
# Option A: cloudflared quick tunnel (no account needed)
cloudflared tunnel --url http://localhost:8787

# Option B: ngrok
ngrok http 8787
```

Both print a public `https://…` URL. That URL plus your token is what you hand
to the remote client. Keep both private — anyone with them can run Codex on
your machine.

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
  -d '{"prompt": "explain what this repo does"}'
```

Response:
```json
{
  "command": "codex",
  "exit_code": 0,
  "stdout": "…final Codex answer…",
  "stderr": "…progress log…",
  "duration_seconds": 12.34
}
```

## Configuration (`config.json`)

| Key | Meaning |
| --- | --- |
| `token` | Shared secret. Required, min 32 chars. |
| `host` / `port` | Bind address. Keep `127.0.0.1`. |
| `default_timeout_seconds` | Fallback per-run timeout (default 600). |
| `workdir` | Optional fixed working directory for the CLI (`null` = wherever the server runs). |
| `allowed_commands` | The only runnable commands. Each maps to a fixed argv prefix; the prompt is appended as one argument. |

Want Codex flags (for example a sandbox mode)? Add them to the argv prefix:

```json
"codex": { "argv": ["codex", "exec", "--sandbox", "read-only"], "timeout_seconds": 900 }
```

## Notes

- `codex exec` prints progress to `stderr` and the final answer to `stdout` —
  the API returns both, separately.
- Long tasks: raise `timeout_seconds` in the allowlist entry rather than
  retrying.
- Rotate the token any time by editing `config.json` and restarting.

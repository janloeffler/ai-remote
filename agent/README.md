# AI Remote — Local Agent

## Setup

    python3 -m venv .venv
    .venv/bin/pip install --require-hashes -r requirements-dev.txt

## Run once, manually, for testing

    AI_REMOTE_BACKEND_URL=https://your-domain.example.com \
    AI_REMOTE_API_KEY=<your key> \
    .venv/bin/python3 -c "from agent.main import load_config, run_cycle; import httpx; c=load_config(); \
    client=httpx.Client(timeout=10); run_cycle(c, client)"

## Install as a background service (launchd)

1. Copy the plist into place FIRST (before editing it):

       cp launchd/com.example.ai-remote-agent.plist ~/Library/LaunchAgents/

2. Edit the COPY at `~/Library/LaunchAgents/com.example.ai-remote-agent.plist` — NOT the
   one in this repo: set the real API key, your own reverse-DNS `Label` if you'd like one
   distinct from the example, and confirm the `.venv` path matches where you cloned this
   repo. Editing the copy (not the tracked repo file) means the real key never risks
   ending up in a future git commit. (Prefer the automated path instead? `./setup-agent.sh`
   does all of this for you, including keeping your real `Label`/API key out of git — see
   the repo-root README's Quickstart.)
3. Load it:

       launchctl load ~/Library/LaunchAgents/com.example.ai-remote-agent.plist

4. Check it's running: `launchctl list | grep ai-remote-agent`
5. Logs: `~/Library/Logs/ai-remote-agent.log` / `.error.log`

## Tests

    .venv/bin/pytest -v

## End-to-end encryption (optional)

With `E2E_ENCRYPTION=true` in the repo-root `.env` (and on the server), `./setup-agent.sh`
asks for the passphrase, derives the key and writes two variables into the plist (mode
`600`): `AI_REMOTE_E2E=true` and `AI_REMOTE_E2E_KEY` (base64 of the derived 32-byte key —
never the passphrase). The setup step on its own:

    AI_REMOTE_BACKEND_URL=https://your-domain.example.com AI_REMOTE_API_KEY=<key> \
    .venv/bin/python -m agent.e2e_setup            # prints the key on stdout
    .venv/bin/python -m agent.e2e_setup --rotate   # debugging only, see below

Rotate the passphrase with `./setup-agent.sh --rotate-passphrase`: it stops the agent,
rotates (the server drops its cache) and updates the plist. The bare
`python -m agent.e2e_setup --rotate` is for debugging only: it leaves the plist with the
old key, so the running agent would stop syncing.

Every cycle starts with `GET /agent/handshake`. The agent sends nothing and claims no jobs
when its mode differs from the server's, when the server has no parameters yet, or when
its key does not match the server's key check — see `agent/handshake.py` and the error
log. When the server's data epoch changes (mode switch, passphrase rotation), the agent
forgets what it synced and pushes everything again.

In E2E mode the agent renders each message to HTML with `agent/render_core.py` (a
byte-identical copy of `backend/app/render_core.py`; a test keeps them in sync) and
encrypts titles, previews, messages, job results and images before they leave the Mac.
Search runs here, as a `search` job. Encrypted prompts carry a random id and timestamp
inside the ciphertext; the agent rejects expired or already-used ones (replay protection,
used ids are kept in `used_prompt_ids.json` next to the sync state). The agent now needs
its Python dependencies (`agent/requirements.txt`) installed even in plaintext mode;
`setup-agent.sh` and `update-agent.sh` do that. Design: `docs/superpowers/specs/2026-10-09-e2e-encryption-design.md`.

## Remote command execution (resume/new-session)

Two things must be configured before the "continue chat" / "new session" features
will do anything:

1. `AI_REMOTE_ALLOWED_PROJECTS` — a comma-separated list of absolute project paths.
   The agent itself has no dotenv loading, so this is **not** set in a `.env` file
   inside `agent/`. Instead, set it in the repo-root `.env` (the same file that holds
   `API_KEY`/`AI_REMOTE_BACKEND_URL`), then run (or re-run) `./setup-agent.sh` from
   the repo root — it sources that `.env` and bakes the value into the generated
   launchd plist's `EnvironmentVariables`, so the running agent sees it as a normal
   environment variable. **Must exactly match** the same-named variable set in the
   backend's `.env` on the server — these are two independently-configured copies,
   checked independently by each side (defense in depth), never synced between them
   and never editable via any API route. Entries must be written in their canonical
   (symlink-resolved) form — e.g. run `cd <dir> && pwd -P` to get the exact string to
   use — since Claude Code records the OS-canonicalized `cwd` and this side never
   normalizes paths itself; this matters especially for paths under `/tmp` or other
   commonly-symlinked locations (on macOS, `/tmp` resolves to `/private/tmp`).
2. Each allow-listed project needs a `permissions.allow`/`deny` profile of its own, in
   the location and **dialect** of the tool that will read it:

   | Tool | File | Rule types | Starter template |
   |---|---|---|---|
   | Claude Code | `<project>/.claude/settings.json` | `Bash(...)`, `Edit(...)`, `Read(...)`, `Write(...)`, `WebFetch(...)`, … | `docs/superpowers/reference/remote-agent-permissions.claude-code.json` |
   | cursor-agent | `<project>/.cursor/cli.json` | `Shell(...)`, `Read(...)`, `Write(...)`, `WebFetch(...)`, `Mcp(...)` | `docs/superpowers/reference/remote-agent-permissions.cursor-cli.json` |

   Both files wrap their rules in the same `{"permissions": {"allow": […], "deny": […]}}`
   envelope, **but the rule types inside are not interchangeable.** Cursor has no
   `Bash(...)` or `Edit(...)`; Claude Code has no `Shell(...)` or `Mcp(...)`. Because
   `cursor-agent -p --force` allows commands *unless explicitly denied*, a Claude Code
   profile pasted into `.cursor/cli.json` denies nothing whatsoever — the file exists,
   so it looks configured, while the job actually runs unconstrained. Copy the template
   that matches the tool and narrow its `allow` list to that specific project.

   **This is enforced, not advisory.** The agent still never writes the file for you,
   but a remote job is refused with `status: failed` — and the CLI is never invoked —
   when the target project's profile is missing, empty, unparseable, or written in the
   other tool's dialect. See `agent/agent/permission_profile.py`. Note the profile is a
   *deny*-oriented list and only scopes what the CLI will do; read `SECURITY.md` before
   allow-listing anything, because remote command execution is this tool's purpose and
   whoever holds `API_KEY` gets it.

Remote command jobs run with a 30-minute subprocess timeout — if a prompt genuinely
needs longer, it will be killed and reported `failed`.

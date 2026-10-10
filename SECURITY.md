# Security

This is a personal, single-user tool. It intentionally has a small, simple threat
model, and one very sharp edge.

**The sharp edge, stated plainly: this application exists to run AI-agent commands
on the machine where you run the agent. Anyone who holds `API_KEY` can do that.**
Commands run as your OS user, in auto-approving mode (`claude --permission-mode
dontAsk` / `cursor-agent --force`), inside whichever projects you allow-listed. That
is the feature, not a bug — but it means the blast radius of a leaked `API_KEY` is
everything that user can reach: source code, SSH keys, cloud credentials, browser
data. Read this whole file before you expose the backend to the internet.

## The security model

- **One shared secret.** `API_KEY` is both the agent's bearer token and the password
  you type into `/login`. It must be at least 32 characters (`openssl rand -hex 32`);
  the backend refuses to start otherwise. `SECRET_KEY` signs the session cookie.
- **Two independent allow-list checks.** Remote commands may only run in paths listed
  in `AI_REMOTE_ALLOWED_PROJECTS`, which is checked separately by the backend and by
  the agent. Neither copy is editable through any API route.
- **Per-project permission profiles, enforced.** A remote job is refused unless the
  target project has a permission profile written in the dialect of the tool that will
  read it — `.claude/settings.json` (`Bash(...)`/`Edit(...)`) for Claude Code,
  `.cursor/cli.json` (`Shell(...)`/`Mcp(...)`) for cursor-agent. The two are **not**
  interchangeable, and a profile using the other tool's rule types is rejected rather
  than accepted as if it constrained anything. See `agent/README.md`.
- **A kill switch and an audit log.** `/settings/pause-remote-commands` stops all
  remote execution; `/jobs` lists every job ever queued with its result.
- **Sessions tied to the key.** The session cookie is signed, `SameSite=Lax`, `Secure` in
  production, and carries an HMAC fingerprint of `API_KEY`: rotating `API_KEY` or
  `SECRET_KEY` invalidates every session, and `/logout` ends the current one.
- **Secure cookies in production.** Both deploy scripts always ship
  `SESSION_COOKIE_HTTPS_ONLY=true`, even though `run.sh` writes `false` into the local `.env`
  for `http://localhost` testing.
- **Hash-pinned dependencies.** Lockfiles with SHA-256 hashes, installed with
  `--require-hashes`; releases younger than 7 days are skipped when locking, and
  `scripts/audit-deps.py` checks the pins against OSV (vulnerabilities, reported malware).
- **No multi-tenant isolation.** There is one user. Every authenticated session sees
  every synced chat.
- **Loopback by default.** `docker-compose.yml` binds `127.0.0.1`. Exposing the
  backend beyond localhost requires a TLS-terminating reverse proxy in front of it
  and `SESSION_COOKIE_HTTPS_ONLY=true`.

A pre-release security audit (2026-08-28) found 0 Critical and 3 High issues, all
three in the remote-execution chain. All three are fixed, along with several
Medium/Low ones. What follows is what that audit found and this project has
deliberately *not* fixed, because doing so would mean a design change out of
proportion to a single-user tool.

## Optional end-to-end encryption (`E2E_ENCRYPTION=true`)

Off by default. When on, the Mac agent encrypts message contents, tool output, images, job
prompts and results, session titles and previews with AES-256-GCM under a key derived from your
passphrase (Argon2id). The server stores and relays ciphertext only; the browser derives the same
key after login and decrypts locally.

**Covered**
- Leaked data files written after E2E is on: backups, snapshots, copied volumes, file-read bugs.
  Plaintext stored before you enabled E2E is not retroactively protected: deleted rows (freed
  SQLite pages) and unlinked image files may remain recoverable from the raw disk or volume.
  Enable E2E on a fresh volume/data directory, or securely wipe the old one.
- A passive attacker with root on the server (reads disk, `.env`, process memory): the server
  never has the key or the passphrase.

**Not covered**
- An active attacker on the server. The server delivers the JavaScript that receives your
  passphrase; someone who modifies it can capture the passphrase at your next login. This is
  inherent to web-delivered E2E.
- `API_KEY` still matters, but less: in E2E mode an attacker who only holds `API_KEY` cannot
  run commands, because prompts must be encrypted with the passphrase-derived key and are
  replay-protected by a one-time id plus a 1 h expiry, enforced on the Mac. Such an attacker can
  still pause/unpause remote commands, queue fetches and searches, and read metadata; the kill
  switch remains. Root on the server can still serve modified JavaScript (active attacker,
  above), which defeats this.
- Metadata stays plaintext: session ids, tool, project paths, image source paths, timestamps,
  message counts, roles, sizes, job type/status/target.
- Old backups and old disks. Switching the mode wipes the server's database (securely, then `VACUUM`), but
  backups made earlier stay plaintext — delete them yourself — and wiped data may still be
  recoverable from the underlying disk (see above).

**Keys**
- On the Mac the derived key (not the passphrase) sits in the launchd plist
  (`AI_REMOTE_E2E_KEY`), file mode `600`. Anyone who can read your user's files can read it —
  as they could already read your chats.
- In the browser a non-extractable key is kept in IndexedDB. It is deleted on logout (also from
  the unlock overlay), on visiting the login page, and when an `API_KEY` / passphrase rotation is
  detected at the next page load. Malicious script running in the page while unlocked can use it.
- A strict Content-Security-Policy applies to all pages (`script-src 'self' 'wasm-unsafe-eval'`,
  `style-src 'self'`, `img-src 'self' blob: data:`), limiting injected script. Child processes
  started by the agent no longer see the `AI_REMOTE_*` environment variables (including the key).
- Passphrase strength matters: the salt and key-check value are served to authenticated
  sessions and the agent, and allow an offline guess attack. Use a long, unique passphrase
  (minimum 16 characters is enforced).
- Fail-closed: if the agent's and server's modes or keys disagree, the agent syncs nothing and
  claims no jobs, and the server rejects plaintext in E2E mode. A plaintext-mode agent declares
  its mode via a header, and the server rejects a mismatch.

## Known limitations

- **One secret serves two principals, and sessions are stateless.** `API_KEY` is the
  agent's bearer token *and* your login password, so it lives both in the launchd plist
  on your Mac and in whatever your phone browser autofills. `/logout` clears the
  session in the browser you click it in, and each session cookie carries an HMAC
  fingerprint of `API_KEY`, so **rotating `API_KEY` invalidates every existing session**
  (as does rotating `SECRET_KEY`). What is still missing is server-side revocation of a
  single session: the cookie is signed, not stored, so a cookie copied off a device
  stays valid for its 14-day lifetime unless you rotate a key. *If this matters to you:*
  rotate `API_KEY` after losing a device, and don't let a browser remember the key.
- **No CSRF tokens on the form POSTs.** `/login` and `/settings/pause-remote-commands`
  are protected only by the session cookie's `SameSite=Lax`, which does block
  cross-site POSTs in every current browser — so this is not exploitable today. It has
  no second layer, though, and the kill switch is a high-value target. *If this matters
  to you:* don't add a CORS policy or serve anything untrusted from a sibling
  subdomain without adding tokens first.

- **The permission profiles are deny-lists, and the shipped templates are not
  exhaustive.** `remote-agent-permissions.claude-code.json` denies `Bash(rm -rf:*)`,
  `curl` and `wget`, but a deny-list can only ever enumerate badness — it does not stop
  `python -c "urllib…"`, and both templates grant broad read/write. The agent requires
  each allow-listed project to have a profile and rejects one written in the other
  tool's dialect, but it cannot judge whether yours is any *good*. *If this matters to
  you:* replace the `allow` list with an explicit, narrow set of tools for that specific
  project rather than tuning the `deny` list.

- **Job history and results grow without bound.** Remote-command prompts are capped at
  32,000 characters, but nothing caps how many jobs you may queue, how large each
  stored `result_text` may be, or prunes completed jobs on a retention schedule. A
  single-user deployment reaches this limit only through deliberate abuse, and the
  failure mode is a large SQLite file rather than a compromise. *If this matters to
  you:* the database is a single file (`data/app.db`) — back it up and prune it
  yourself.

- **Out of the box, an attacker can lock you out of `/login` for five minutes.** The
  login throttle ignores `X-Forwarded-For` unless you configure `TRUSTED_PROXY_HOPS`
  *and* `TRUSTED_PROXIES` together. Until you do, every request behind a reverse proxy
  arrives from the proxy's address, so all login attempts — yours and an attacker's —
  share one bucket and sustained wrong guesses throttle you too. There is deliberately
  no global cap on failures across buckets: such a cap would make that lockout
  unavoidable even with per-client buckets configured, handing anyone a repeatable way
  to deny you access. Memory is bounded by evicting stale buckets instead, which never
  blocks a login. *If this matters to you:* set `TRUSTED_PROXY_HOPS=1` and
  `TRUSTED_PROXIES=<your proxy's peer address as the container sees it>`, and make sure
  the container port is not reachable except through that proxy — otherwise a direct
  connection can forge the header and buy fresh buckets.

- **Dependencies are hash-pinned, but nothing audits them for you.** Both
  `backend/requirements.txt` and `agent/requirements.txt` are lockfiles generated by
  `uv` from the adjacent `requirements.in` (run `./relock.sh`), every package carries
  its SHA-256 hashes, and the Docker build and the install scripts use
  `pip install --require-hashes`, so a tampered or substituted package is refused.
  Dependabot (`.github/dependabot.yml`) opens weekly update PRs on GitHub. `./relock.sh` and
  Dependabot both ignore releases younger than 7 days, since most malicious uploads are
  caught within days. `python3 scripts/audit-deps.py` checks the lockfiles against OSV
  (known vulnerabilities and reported malware) and PyPI (yanked or very new releases).
  What remains is the usual supply-chain residue: an *unreported* malicious release older
  than the cooldown would still be installed, and nothing runs the audit automatically.
  *If this matters to you:* review Dependabot PRs instead of auto-merging them, and run
  the audit before every release.
- **The container runs as root.** `backend/Dockerfile` has no `USER` directive, so
  uvicorn runs as root inside the container and writes the mounted SQLite volume as
  root. There is no known escape, and the container is meant to sit behind a reverse
  proxy on loopback — but it removes a mitigating layer if a flaw in the web tier is
  ever found. Adding a non-root user requires re-owning existing `/data` volumes, which
  is why it is documented rather than changed under your feet. *If this matters to
  you:* add `RUN useradd -r app` / `USER app` and `chown` the volume before restarting.

- **Deployment authenticates as `root`.** `deploy-to-plesk.sh` SSHes in as `root`, so a
  compromised deploy key means full server control rather than container-scoped access.
  The `.env` it ships is now written mode `0600`. *If this matters to you:* deploy as a
  dedicated user with only the docker permissions it needs.

## If you deploy this yourself

1. Generate `API_KEY` and `SECRET_KEY` with `./generate-secrets.sh` (256-bit random values, appended to `.env` and never printed; `--rotate` replaces existing ones). Never reuse a
   password you have typed anywhere else.
2. Keep the backend behind a TLS-terminating reverse proxy, on loopback, with
   `SESSION_COOKIE_HTTPS_ONLY=true`.
3. Allow-list the smallest set of projects you actually need, and give each one a
   permission profile you have read line by line.
4. Rotate both secrets if either has ever been printed to a terminal, pasted into a
   chat, or captured in an AI-assistant transcript.

## Reporting a vulnerability

Open a GitHub issue, or contact the maintainer directly for anything sensitive.
Please don't file exploit details in a public issue before there's a fix.

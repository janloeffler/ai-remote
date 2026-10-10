# Optional end-to-end encryption (E2E) of chat content

**Date:** 2026-10-09
**Status:** Approved (grilling session 2026-10-09)

## Problem

Everything the backend stores is plaintext: `data/app.db` holds the session index, the
message cache, a FTS5 full-text index, job prompts and job results; chat images are plain
files in `IMAGE_DIR`. Anyone who obtains those files — a backup, a Plesk snapshot, a copied
volume, a retired disk, a path-traversal bug — or who gets passive root on the server can
read every synced chat.

Encrypting with a key from the server's `.env` does not help against root: the key sits
next to the data and in the container's memory. The only design that holds against a
passive root attacker is one where **the server never has the key**.

## Threat model

| Attacker | Covered? |
|---|---|
| A — data files leak without `.env` (backup, snapshot, volume, disk, file-read bug) | **Yes** |
| B — passive root on the server (reads disk, `.env`, process memory) | **Yes** |
| C — active root on the server (modifies code/JS, waits for next login) | **No** — the server delivers the JS that receives the passphrase. Inherent to web E2E. |
| D — hoster/admin as reader | Same as A/B |

**Remote execution:** in E2E mode an `API_KEY`-only attacker cannot run commands: prompts must
be encrypted with the passphrase-derived key and carry a one-time id plus a timestamp (1 h
expiry), enforced on the Mac. Such an attacker can still pause/unpause remote commands, queue
fetches and searches and read metadata; the kill switch remains. Root on the server can still
serve modified JS (attacker C, out of scope).

## Decisions (from the grilling session)

1. **E2E between Mac agent and browser.** The server stores and relays ciphertext only.
   Optional: `E2E_ENCRYPTION=true|false` (default `false`). With `false` the user-visible
   behavior is as before and the browser loads no E2E assets. The only changes for the agent in
   plaintext mode: it performs a handshake per cycle and one full resync after upgrade; no
   other behavior change.
2. **Search in E2E mode is an agent job.** The Mac searches its local plaintext and returns
   encrypted matching session ids. In plaintext mode FTS5 search stays as is.
3. **Key from a passphrase via Argon2id.** Salt, KDF parameters and a key-check value live
   on the server (non-secret). The passphrase is entered in the browser **after** the
   `API_KEY` login (see "Why not on the login page").
4. **Encrypted:** message contents, tool output, images, job prompts (resume, new session,
   search query), `result_text`, session titles and previews.
   **Plaintext metadata:** session ids, tool, entrypoint, **project paths**, timestamps,
   message counts, roles, sizes, job type/status/target, **image source paths** (same class
   as project paths; needed to queue `fetch_image` jobs). The backend's allow-list check
   stays unchanged.
5. **Mode switch = drop server cache + resync.** Job history is lost on every switch (logged
   at startup). After the wipe: `secure_delete`, FTS table dropped and recreated, `VACUUM`,
   WAL truncated, verified. Old backups stay plaintext — the operator must delete them.
6. **One direct key, no envelope.** AES-256-GCM, random 96-bit nonce per record, AAD binds
   record type + identity, version prefix in the ciphertext. Passphrase change = new salt,
   new key, wipe + resync.
7. **Agent renders HTML** with the very same Python module as the backend
   (`render_core.py`, byte-identical copy in the agent, enforced by a test). The browser
   decrypts and sanitizes again with **DOMPurify** before inserting.
8. **On the Mac** the derived master key (never the passphrase) is stored in the launchd
   plist (`AI_REMOTE_E2E_KEY`), file mode `600`.
9. **In the browser** a non-extractable `CryptoKey` lives in IndexedDB until logout or
   until logout, a visit to the login page, or `API_KEY` / passphrase rotation (detected at the
   next page load).
10. **Both sides configure the mode explicitly, fail-closed.** The agent compares its own
    `AI_REMOTE_E2E` with the server's mode and its key with the server's key check on every
    cycle; on any mismatch it sends nothing and claims no jobs. The backend independently
    rejects plaintext in E2E mode and ciphertext in plaintext mode.
11. **Acceptance:** all six test points (see "Tests").

### Why not on the login page

The salt and key-check value allow an offline dictionary attack against the passphrase.
Publishing them on the unauthenticated login page would hand them to anyone on the
internet. They are therefore only served to authenticated sessions (and to the agent),
and the passphrase prompt appears right after the `API_KEY` login.

## Cryptography

### Key derivation

```
master   = Argon2id(passphrase_utf8, salt, m=65536 KiB, t=3, p=1, len=32)
enc_key  = HKDF-SHA256(master, salt="", info="ai-remote/v1/enc",       len=32)  → AES-256-GCM
check    = HKDF-SHA256(master, salt="", info="ai-remote/v1/key-check", len=32)  → hex, 64 chars
```

- `salt`: 16 random bytes, base64 (standard, padded) on the wire.
- KDF parameters are stored with the salt as JSON
  `{"alg":"argon2id","m":65536,"t":3,"p":1,"v":19}` so they can change later. Accepted
  ranges on the server: `m` 19456–1048576, `t` 2–10, `p` 1–4.
- Agent: `argon2-cffi` (`argon2.low_level.hash_secret_raw`, `Type.ID`) + `cryptography`
  (`HKDF`, `AESGCM`). Browser: vendored `hash-wasm` (argon2id) + WebCrypto (HKDF, AES-GCM).
- Passphrase minimum length: 16 characters (enforced when it is set).

### Ciphertext format

```
"e2e1:" + base64url_nopad( nonce[12] || AES-GCM(enc_key, nonce, plaintext_utf8_or_bytes, aad) )
```

The `e2e1:` prefix is the version marker. Binary payloads (images) are stored as raw
bytes `b"e2e1" || nonce[12] || ct+tag` (4-byte magic instead of the text prefix); the agent
sends them base64-encoded in `data_b64` as today.

### AAD (UTF-8 strings)

| Record | AAD | Plaintext |
|---|---|---|
| Session title | `session\|<session_id>\|title` | title text |
| Session preview | `session\|<session_id>\|preview` | JSON `{"text": str, "html": str}` |
| Message | `msg\|<session_id>\|<idx>` | rendered, sanitized HTML |
| Resume prompt | `job-prompt\|resume_message\|<session_id>` | prompt envelope (below) |
| New-session prompt | `job-prompt\|new_session\|<project_path>\|<tool>` | prompt envelope (below) |
| Search query | `job-prompt\|search` | query text |
| Job result | `job-result\|<job_id>` | result text (for `search`: JSON `{"ids": [...]}`) |
| Image | `image\|<session_id>\|<path_key>` | raw image bytes |

**Prompt envelope** (replay protection): the plaintext of resume and new-session prompts is the
JSON `{"v":1,"prompt":<str>,"rid":<32 lowercase hex, 16 random bytes>,"ts":<ms since epoch>}`.
The agent accepts each `rid` once and rejects envelopes older than 1 h, so an `API_KEY` holder
cannot replay or forge prompts. The jobs page shows `prompt` (raw text if not an envelope).

AAD prevents the server from swapping ciphertexts between records (e.g. moving a prompt to
another project). That is an active attack and out of scope, but it costs nothing.

## Server state

New table:

```sql
CREATE TABLE IF NOT EXISTS e2e_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    mode TEXT,            -- 'plain' | 'e2e'; NULL = legacy DB = 'plain'
    salt TEXT,            -- base64, NULL until the agent setup sets it
    kdf TEXT,             -- JSON
    key_check TEXT,       -- 64 hex
    data_epoch TEXT NOT NULL  -- random id, regenerated on every wipe
);
```

### Startup

`init_db` → `e2e.reconcile_mode(conn, settings.E2E_ENCRYPTION)`:

- stored mode (NULL → `plain`) equals configured mode → nothing.
- otherwise → **wipe**, store new mode, new `data_epoch`, log a warning:
  "E2E mode changed plain→e2e: server cache dropped, job history deleted, agent will
  resync. Delete old backups of data/ yourself — they still contain plaintext."

### Wipe (`e2e.wipe_content`)

1. `PRAGMA secure_delete = ON` for the connection.
2. `DELETE FROM messages; DELETE FROM sessions; DELETE FROM jobs; DELETE FROM images;`
3. `DROP TABLE search_index` and recreate it from `schema.sql` (FTS5 deletes leave
   tombstoned plaintext in segment shadow tables; dropping removes them).
4. Delete every file in `IMAGE_DIR` (not the directory).
5. Commit, `VACUUM`, `PRAGMA wal_checkpoint(TRUNCATE)`.
6. Verify: counts are 0 and `PRAGMA freelist_count` is 0; otherwise raise (container must
   not start half-wiped).

`agent_status`, `settings`, `e2e_state` (salt/kdf/key_check) are kept. A mode switch
e2e→plain→e2e therefore keeps the passphrase valid.

## API contract

### Agent endpoints (bearer `API_KEY`)

`GET /agent/handshake` →
```json
{"e2e": true, "epoch": "<data_epoch>", "salt": "<b64>|null", "kdf": {...}|null, "key_check": "<hex>|null"}
```
In plaintext mode: `{"e2e": false, "epoch": "...", "salt": null, "kdf": null, "key_check": null}`.

`PUT /agent/e2e-params` body `{"salt": b64, "kdf": {...}, "key_check": hex, "reset": bool}`:
- 409 when the server is in plaintext mode.
- 422 on invalid salt (16–64 bytes), kdf (alg/ranges) or key_check (64 lowercase hex).
- Params unset → store, 200.
- Params set and identical → 200 (idempotent).
- Params set and different: `reset=false` → 409; `reset=true` → wipe + new epoch + store (params are set NULL during the wipe, so a
  failed wipe leaves the server unconfigured, not half-rotated).
- Returns the handshake JSON.

### Ingest validation in E2E mode

Ciphertext check: `value.startswith("e2e1:")` and the rest matches `[A-Za-z0-9_-]{38,}`
(12-byte nonce + 16-byte tag minimum).

- `POST /sync/index`: `title`, `last_message_preview`, every `recent_messages[].content`
  must be ciphertext → else 422. No FTS writes at all.
- `POST /jobs/{id}/complete`: `messages[].content` must be ciphertext; `result_text` must be
  empty or ciphertext → else 422.
- `POST /sync/image`: body unchanged (`session_id`, `path`, `data_b64`); the decoded bytes
  must start with the magic `b"e2e1"` (else 415) and are stored as-is (no mime sniffing
  possible), mime recorded as `application/octet-stream`, file `<key>.bin`, size cap
  `IMAGE_MAX_BYTES + 32`. In plaintext mode bytes starting with `b"e2e1"` already fail the
  existing magic-number check.
- **Mode declaration, no content sniffing.** The agent sends `X-AI-Remote-E2E: 1` (E2E) or
  `0` (plaintext) on `POST /sync/index`, `POST /sync/image` and `POST /jobs/{id}/complete`.
  A plaintext server answers 409 ("agent is in E2E mode but the server is not") when the
  header is `1`; an E2E server answers 409 when it is `0`. A missing header (legacy agent)
  is accepted. In plaintext mode content is never inspected — a chat or prompt may
  legitimately begin with `e2e1:`. Browser prompts/search in plaintext mode have no prefix check.
- E2E prompt cap: the ciphertext may be up to 171,000 characters (room for 32,000
  plaintext characters at worst-case 4 UTF-8 bytes each); the browser enforces the
  32,000-character plaintext limit.

### Browser endpoints in E2E mode

- `POST /chats/{id}/command` and `POST /projects/command`: `prompt` must be ciphertext
  (max length 171,000 characters of ciphertext) → else 422. Plaintext mode unchanged (max 32,000).
- `POST /search` (new, session auth) `{"query": ciphertext}` → 409 if a search is already
  pending/running; otherwise creates job `search`
  (target `*`, payload `{"query": ciphertext}`), returns `{job_id, eta_seconds}`. 409 in
  plaintext mode. Not affected by the remote-command kill switch (read-only, like
  `fetch_full`).
- `POST /` with form fields `ids` (comma-separated session ids, max 100), `tool`, `project`,
  `group`, `sort`: restricts the list to those ids (ids never appear in the URL) (both modes; harmless metadata). In E2E mode `q` is ignored server-side and
  `sort=title_asc` falls back to `date_desc` (the option is hidden).
- `POST /chats/{id}/fetch-image`: in E2E mode the "path appears in a message" check is
  skipped (the server cannot read messages); `is_image_path` still applies. The agent keeps
  its own check (path must occur in the session's plaintext), which is the one that guards
  the file read.
- `GET /chats/{id}/images/{key}`: in E2E mode served as `application/octet-stream` with the
  same security headers; the browser decrypts.
- `GET /jobs/{id}/status`: unchanged; `result_text` may be ciphertext.

### Jobs

New job type `search` (`schema.sql` CHECK, migrated like `fetch_image`), timeout 300 s.

## Agent

Config (env, baked into the plist by `setup-agent.sh`):
- `AI_REMOTE_E2E` = `true|false` (from `E2E_ENCRYPTION` in the repo-root `.env`).
- `AI_REMOTE_E2E_KEY` = base64 of the 32-byte master key (only when E2E is on).

Every cycle starts with `GET /agent/handshake`:

| Situation | Behavior |
|---|---|
| handshake fails (network/5xx) | skip the cycle (no sync, no jobs) |
| 404 (old server) and agent plaintext | proceed as today |
| 404 and agent E2E | stop: log error, nothing sent |
| `e2e` ≠ agent mode | stop: log error, nothing sent, no jobs claimed |
| agent E2E, server params null | stop: "run setup-agent.sh" |
| agent E2E, `check(key)` ≠ `key_check` | stop: "wrong key / passphrase rotated" |
| `epoch` ≠ stored epoch | clear `sync_state.json` + `images_state.json`, store epoch → full resync |

"Stop" means: neither `/sync/*` nor `/jobs/pending` is called (claiming would mark jobs
`running` that then never complete).

In E2E mode the agent, on copies (never the cached session dicts):
- encrypts `title`, `last_message_preview` (JSON text+html), each message as
  `render_markdown(content, ImageContext(session_id, available=set()))` → HTML;
- `fetch_full`: same for returned messages;
- `resume_message` / `new_session`: decrypts the prompt with the AAD above (failure → job
  failed "cannot decrypt prompt"), encrypts `result_text` with `job-result|<id>`;
- `search`: decrypts the query, scans all sessions of enabled tools (title, preview, full
  messages; case-insensitive substring), returns up to 100 ids ordered by
  `last_updated_at` desc, encrypted;
- images (pasted upload and `fetch_image`): encrypts bytes with
  `image|<session_id>|<path_key>`; `path_key` from `render_core.path_key`.

### Setup CLI `python -m agent.e2e_setup [--rotate]`

Reads `AI_REMOTE_BACKEND_URL`, `AI_REMOTE_API_KEY`, optional existing `AI_REMOTE_E2E_KEY`.
Prints the base64 master key to stdout (only that); prompts via `getpass` on the TTY.

- Server not in E2E → exit 1 with a clear message.
- Existing key given and it matches the server's key check → print it, done.
- Server params unset → prompt passphrase twice (≥ 16 chars, equal), random salt,
  derive, `PUT` params, print key.
- Server params set → prompt once, derive, compare check → match: print key; else exit 1.
- `--rotate` → prompt new passphrase twice, new salt, `PUT` with `reset=true` (server
  wipes, epoch changes), print key.

`setup-agent.sh`: when `E2E_ENCRYPTION=true`, reuses the key from the installed plist
(`PlistBuddy`) if present, runs the setup CLI, writes `AI_REMOTE_E2E=true` and
`AI_REMOTE_E2E_KEY` into the plist; `chmod 600` on the plist in all cases.

## Browser

Assets, loaded **only in E2E mode**: `static/vendor/hash-wasm-argon2.umd.min.js`,
`static/vendor/purify.min.js` (exact versions + SHA-256 in `static/vendor/VENDOR.md`),
`static/e2e-core.js` (pure crypto, UMD: browser global + Node `module.exports`),
`static/e2e.js` (DOM integration).

Authenticated pages embed `<script type="application/json" id="e2e-config">` with
`{enabled, salt, kdf, key_check, binding}`; `binding` = HMAC(SECRET_KEY,
"e2e-binding:" + API_KEY) so an `API_KEY` rotation invalidates stored keys.

Flow:
1. No usable key in IndexedDB (missing, or stored `binding`/`key_check` differ) → unlock
   overlay: passphrase field. Derive, compare check, store `{key: CryptoKey (AES-GCM,
   non-extractable), key_check, binding}`. Wrong passphrase → error, retry.
2. Params null (agent not set up) → banner, no overlay.
3. Decrypt every `[data-e2e]` element (attributes: ciphertext, `data-e2e-aad`,
   `data-e2e-kind` ∈ `text|html|preview-text|preview-html`). HTML goes through DOMPurify
   with an allow-list equal to the bleach list plus `button` and `data-*` attributes; never
   `img`, `style`, `on*`.
4. After decryption: `window.e2eReady` resolves; app.js runs its content initializers
   (table sorting, copy buttons) after it. In plaintext mode `e2eReady` is undefined
   (e2e.js is not loaded) and app.js falls back to an already resolved promise.
5. Images: buttons whose `path_key` (SHA-256 of `session_id\0path`, first 32 hex) is in
   the page's available-keys list are loaded automatically; others on click as today.
   Bytes are fetched, decrypted, mime sniffed from magic bytes, shown via `blob:` URL.
6. Command / new-session / search forms encrypt before POST. The search box has no `name`
   in E2E mode so the query never reaches a URL or access log. Search results: job →
   decrypt ids → navigate to `/?ids=…` (plus non-secret filters); the query is kept in
   `sessionStorage` to refill the box.
7. Logout (`form[action="/logout"]`) deletes the IndexedDB record before submitting.
8. Jobs page: payload prompt/query and `result_text` decrypted in place.

## Tests (acceptance)

1. **Parametrized backend tests:** existing tests unchanged and green in plaintext mode; an
   `e2e` fixture runs the content-bearing route tests in E2E mode.
2. **Canary:** E2E ingest of title, preview, messages, job prompt, result, image carrying
   `CANARY-7f3a` only inside ciphertext → after `VACUUM` the raw bytes of `app.db`,
   `app.db-wal` and every file in `IMAGE_DIR` do not contain the marker. Agent side: every
   outbound request body in E2E mode is captured and must not contain the marker.
3. **Switch:** plain with canary data → restart in E2E → no marker in any file; e2e → plain
   → tables empty, epoch changed.
4. **Fail-closed (agent):** mode mismatch, wrong key check, missing params, missing key,
   404 handshake in E2E → zero `/sync/*` and `/jobs/pending` calls.
5. **Crypto interop:** fixed vectors (passphrase, salt, kdf → master, check; key + nonce +
   AAD + plaintext → ciphertext) generated by Python, verified by Python and by Node
   running `e2e-core.js` with the vendored argon2 (`node --test`, invoked from pytest).
6. **Browser end-to-end** with Playwright against a local backend in E2E mode fed by the
   real agent code: unlock, read chat, image, search, send prompt, logout clears the key.

## Non-goals

- Protection against an active server attacker (C).
- Encrypting project paths, image paths or other metadata.
- Moving `API_KEY` into the Keychain.
- Server-side search, title sort or FTS in E2E mode.
- Preserving job history across mode switches.

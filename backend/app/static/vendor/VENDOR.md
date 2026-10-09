# Vendored browser libraries

Loaded only when `E2E_ENCRYPTION=true` (see `docs/superpowers/specs/2026-10-09-e2e-encryption-design.md`).
Served from this origin on purpose: a CDN would be one more party able to swap the code
that receives the passphrase.

| File | Package | Version | Source file | License | SHA-256 |
|---|---|---|---|---|---|
| `hash-wasm-argon2.umd.min.js` | [hash-wasm](https://www.npmjs.com/package/hash-wasm) | 4.12.0 (2024-11-19) | `dist/argon2.umd.min.js` | MIT | `dcec617a2e1b700fa132d1583a186cb70611113395e869f2dd6cc82b415d3094` |
| `purify.min.js` | [dompurify](https://www.npmjs.com/package/dompurify) | 3.4.16 (2026-09-23) | `dist/purify.min.js` | Apache-2.0 / MPL-2.0 | `2c90a9b46d6463f26038a29b686e82bc91de01fdac9d5229e7cfe3b360134ea2` |

Both taken unmodified from the npm tarballs (`npm pack <pkg>@<version>`, registry
integrity verified by npm). To update: download the new tarball, copy the same file,
update this table, and run `cd backend && .venv/bin/pytest tests/test_e2e_js.py`.

Verify: `shasum -a 256 backend/app/static/vendor/*.js`

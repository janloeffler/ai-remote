import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const core = require(path.join(here, "../../app/static/e2e-core.js"));
const hashwasm = require(path.join(here, "../../app/static/vendor/hash-wasm-argon2.umd.min.js"));
const vectors = JSON.parse(
  readFileSync(path.join(here, "../../../agent/tests/fixtures/e2e_vectors.json"), "utf8")
);

const keysFromMaster = (b64) => core.importKeys(core.b64ToBytes(b64));

for (const c of vectors.kdf_cases) {
  test(`kdf ${c.name}: master and check match the agent`, async () => {
    const master = await core.deriveMaster(c.passphrase, c.salt_b64, c.kdf, hashwasm.argon2id);
    assert.equal(core.bytesToB64(master), c.master_b64);
    const { check } = await core.importKeys(master);
    assert.equal(check, c.check_hex);
    assert.ok(master.every((b) => b === 0), "master bytes zeroed");
  });
}

for (const c of vectors.cipher_cases) {
  test(`cipher ${c.name}: exact ciphertext and round trip`, async () => {
    const { encKey } = await keysFromMaster(c.master_b64);
    const ct = await core.encryptText(encKey, c.plaintext, c.aad, core.b64ToBytes(c.nonce_b64));
    assert.equal(ct, c.ciphertext);
    assert.equal(await core.decryptText(encKey, c.ciphertext, c.aad), c.plaintext);
    await assert.rejects(core.decryptText(encKey, c.ciphertext, c.aad + "x"));
  });
}

for (const c of vectors.binary_cases) {
  test(`binary ${c.name}: decrypt and encrypt`, async () => {
    const { encKey } = await keysFromMaster(c.master_b64);
    const bytes = core.b64ToBytes(c.bytes_b64);
    const blob = core.b64ToBytes(c.ciphertext_b64);
    assert.deepEqual(await core.decryptBytes(encKey, blob, c.aad), bytes);
    const again = await core.encryptBytes(encKey, bytes, c.aad, core.b64ToBytes(c.nonce_b64));
    assert.deepEqual(again, blob);
    await assert.rejects(core.decryptBytes(encKey, blob, "image|other|key"));
  });
}

test("isCiphertext parity", () => {
  const body = "A".repeat(38);
  assert.equal(core.isCiphertext("e2e1:" + body), true);
  assert.equal(core.isCiphertext("e2e1:" + body + "_-"), true);
  assert.equal(core.isCiphertext("e2e1:" + "A".repeat(37)), false);
  assert.equal(core.isCiphertext("e2e1:" + body + "\n"), false);
  assert.equal(core.isCiphertext("e2e2:" + body), false);
  assert.equal(core.isCiphertext(body), false);
  assert.equal(core.isCiphertext("e2e1:" + body + "="), false);
  assert.equal(core.isCiphertext(null), false);
});

test("pathKey matches the Python rule", async () => {
  // python3 -c "import hashlib;print(hashlib.sha256(b'claude-code:abc\0/tmp/x.png').hexdigest()[:32])"
  const expected = "86ba432b93d72874f76226a836ea781a";
  assert.equal(await core.pathKey("claude-code:abc", "/tmp/x.png"), expected);
});

test("sniffImageMime", () => {
  assert.equal(core.sniffImageMime(Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0])), "image/png");
  assert.equal(core.sniffImageMime(Uint8Array.from([0xff, 0xd8, 0xff, 0xe0])), "image/jpeg");
  assert.equal(core.sniffImageMime(new TextEncoder().encode("GIF89a....")), "image/gif");
  assert.equal(core.sniffImageMime(new TextEncoder().encode("RIFF\0\0\0\0WEBPVP8 ")), "image/webp");
  assert.equal(core.sniffImageMime(new TextEncoder().encode("<svg xmlns=''/>")), null);
  assert.equal(core.sniffImageMime(new Uint8Array(0)), null);
});

test("aad helpers follow the spec table", () => {
  assert.equal(core.aad.title("s"), "session|s|title");
  assert.equal(core.aad.preview("s"), "session|s|preview");
  assert.equal(core.aad.message("s", 3), "msg|s|3");
  assert.equal(core.aad.resumePrompt("s"), "job-prompt|resume_message|s");
  assert.equal(core.aad.newSessionPrompt("/p", "codex"), "job-prompt|new_session|/p|codex");
  assert.equal(core.aad.searchQuery(), "job-prompt|search");
  assert.equal(core.aad.jobResult("j"), "job-result|j");
  assert.equal(core.aad.image("s", "k"), "image|s|k");
});

test("prompt envelope: shape, unique rid, round trip, fallback", () => {
  const a = JSON.parse(core.makePromptEnvelope("héllo"));
  const b = JSON.parse(core.makePromptEnvelope("héllo"));
  assert.deepEqual(Object.keys(a).sort(), ["prompt", "rid", "ts", "v"]);
  assert.equal(a.v, 1);
  assert.equal(a.prompt, "héllo");
  assert.match(a.rid, /^[0-9a-f]{32}$/);
  assert.notEqual(a.rid, b.rid);
  assert.ok(Math.abs(Date.now() - a.ts) < 5000);
  assert.equal(core.parsePromptEnvelope(core.makePromptEnvelope("x\ny")), "x\ny");
  assert.equal(core.parsePromptEnvelope("plain text"), "plain text");
  assert.equal(core.parsePromptEnvelope('{"v":2,"prompt":"p"}'), '{"v":2,"prompt":"p"}');
  assert.equal(core.parsePromptEnvelope("123"), "123");
});

test("deriveMaster rejects out-of-range kdf and bad salts", async () => {
  const salt = core.bytesToB64(new Uint8Array(16));
  const ok = { alg: "argon2id", v: 19, m: 19456, t: 2, p: 1 };
  const never = async () => {
    throw new Error("argon2 must not run");
  };
  const stub = async () => new Uint8Array(32);
  await core.deriveMaster("pw", salt, ok, stub);
  const bad = [
    { ...ok, alg: "argon2i" }, { ...ok, v: 16 }, { ...ok, m: 19455 }, { ...ok, m: 1048577 },
    { ...ok, t: 1 }, { ...ok, t: 11 }, { ...ok, p: 0 }, { ...ok, p: 5 }, { ...ok, m: "19456" }, null,
  ];
  for (const kdf of bad) await assert.rejects(core.deriveMaster("pw", salt, kdf, never));
  for (const s of [core.bytesToB64(new Uint8Array(15)), core.bytesToB64(new Uint8Array(65)), "!!!", null]) {
    await assert.rejects(core.deriveMaster("pw", s, ok, never));
  }
  await core.deriveMaster("pw", core.bytesToB64(new Uint8Array(64)), { ...ok, m: 1048576, t: 10, p: 4 }, stub);
});

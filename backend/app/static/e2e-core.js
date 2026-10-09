// Pure E2E crypto core (no DOM). Browser global `E2ECore`; Node `module.exports`.
// Formats and AADs: docs/superpowers/specs/2026-10-09-e2e-encryption-design.md
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.E2ECore = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const subtle = () => globalThis.crypto.subtle;
  const enc = new TextEncoder();
  const dec = new TextDecoder("utf-8", { fatal: true });
  const TEXT_PREFIX = "e2e1:";
  const MAGIC = [0x65, 0x32, 0x65, 0x31]; // "e2e1"
  const NONCE_LEN = 12;
  const CIPHERTEXT_RE = /^[A-Za-z0-9_-]{38,}$/;

  function b64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function bytesToB64(bytes) {
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(bin);
  }

  function bytesToB64url(bytes) {
    return bytesToB64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function b64urlToBytes(text) {
    let b64 = text.replace(/-/g, "+").replace(/_/g, "/");
    while (b64.length % 4) b64 += "=";
    return b64ToBytes(b64);
  }

  function bytesToHex(bytes) {
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  async function deriveMaster(passphrase, saltB64, kdf, argon2id) {
    if (!kdf || kdf.alg !== "argon2id") throw new Error("unsupported kdf");
    return argon2id({
      password: enc.encode(passphrase),
      salt: b64ToBytes(saltB64),
      parallelism: kdf.p,
      iterations: kdf.t,
      memorySize: kdf.m,
      hashLength: 32,
      outputType: "binary",
    });
  }

  async function hkdf(masterKey, info, usages, bits) {
    const params = { name: "HKDF", hash: "SHA-256", salt: new Uint8Array(0), info: enc.encode(info) };
    if (bits) return new Uint8Array(await subtle().deriveBits(params, masterKey, 256));
    return subtle().deriveKey(params, masterKey, { name: "AES-GCM", length: 256 }, false, usages);
  }

  // Consumes (zeroes) masterBytes.
  async function importKeys(masterBytes) {
    try {
      const master = await subtle().importKey("raw", masterBytes, "HKDF", false, ["deriveKey", "deriveBits"]);
      const encKey = await hkdf(master, "ai-remote/v1/enc", ["encrypt", "decrypt"], false);
      const check = bytesToHex(await hkdf(master, "ai-remote/v1/key-check", null, true));
      return { encKey, check };
    } finally {
      masterBytes.fill(0);
    }
  }

  async function seal(key, bytes, aad, nonce) {
    nonce = nonce || globalThis.crypto.getRandomValues(new Uint8Array(NONCE_LEN));
    const ct = new Uint8Array(
      await subtle().encrypt({ name: "AES-GCM", iv: nonce, additionalData: enc.encode(aad) }, key, bytes)
    );
    const out = new Uint8Array(NONCE_LEN + ct.length);
    out.set(nonce, 0);
    out.set(ct, NONCE_LEN);
    return out;
  }

  async function open(key, blob, aad) {
    if (blob.length < NONCE_LEN + 16) throw new Error("ciphertext too short");
    const nonce = blob.subarray(0, NONCE_LEN);
    const ct = blob.subarray(NONCE_LEN);
    return new Uint8Array(
      await subtle().decrypt({ name: "AES-GCM", iv: nonce, additionalData: enc.encode(aad) }, key, ct)
    );
  }

  async function encryptText(key, text, aad, nonce) {
    return TEXT_PREFIX + bytesToB64url(await seal(key, enc.encode(text), aad, nonce));
  }

  async function decryptText(key, value, aad) {
    if (!isCiphertext(value)) throw new Error("not ciphertext");
    return dec.decode(await open(key, b64urlToBytes(value.slice(TEXT_PREFIX.length)), aad));
  }

  async function encryptBytes(key, bytes, aad, nonce) {
    const sealed = await seal(key, bytes, aad, nonce);
    const out = new Uint8Array(4 + sealed.length);
    out.set(MAGIC, 0);
    out.set(sealed, 4);
    return out;
  }

  async function decryptBytes(key, bytes, aad) {
    if (bytes.length < 4 || MAGIC.some((m, i) => bytes[i] !== m)) throw new Error("bad magic");
    return open(key, bytes.subarray(4), aad);
  }

  function isCiphertext(value) {
    return typeof value === "string" && value.startsWith(TEXT_PREFIX) && CIPHERTEXT_RE.test(value.slice(TEXT_PREFIX.length));
  }

  async function pathKey(sessionId, path) {
    const digest = await subtle().digest("SHA-256", enc.encode(sessionId + "\0" + path));
    return bytesToHex(new Uint8Array(digest)).slice(0, 32);
  }

  function sniffImageMime(bytes) {
    const at = (offset, sig) => sig.every((b, i) => bytes[offset + i] === b);
    if (bytes.length >= 8 && at(0, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) return "image/png";
    if (bytes.length >= 3 && at(0, [0xff, 0xd8, 0xff])) return "image/jpeg";
    if (bytes.length >= 6 && at(0, [0x47, 0x49, 0x46, 0x38]) && (bytes[4] === 0x37 || bytes[4] === 0x39) && bytes[5] === 0x61) return "image/gif";
    if (bytes.length >= 12 && at(0, [0x52, 0x49, 0x46, 0x46]) && at(8, [0x57, 0x45, 0x42, 0x50])) return "image/webp";
    return null;
  }

  const aad = {
    title: (sid) => `session|${sid}|title`,
    preview: (sid) => `session|${sid}|preview`,
    message: (sid, idx) => `msg|${sid}|${idx}`,
    resumePrompt: (sid) => `job-prompt|resume_message|${sid}`,
    newSessionPrompt: (projectPath, tool) => `job-prompt|new_session|${projectPath}|${tool}`,
    searchQuery: () => "job-prompt|search",
    jobResult: (jobId) => `job-result|${jobId}`,
    image: (sid, key) => `image|${sid}|${key}`,
  };

  function constantTimeEqual(a, b) {
    if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
    let diff = 0;
    for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
    return diff === 0;
  }

  return {
    b64ToBytes, bytesToB64, bytesToB64url, b64urlToBytes, bytesToHex,
    deriveMaster, importKeys, encryptText, decryptText, encryptBytes, decryptBytes,
    isCiphertext, pathKey, sniffImageMime, aad, constantTimeEqual,
  };
});

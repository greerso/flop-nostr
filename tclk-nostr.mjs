#!/usr/bin/env node
// tclk/1 paper deal on Nostr. Same choreography as Thang's deal.mjs, venue is relays.
// Stay in tclk-offers. Do not open a derived deal room.
import { randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import {
  applyFrame, decodeFrame, encodeFrame, generateHashLock, makeAccept, makeOffer, openContract,
  PaperRail, paperNote,
} from "@flop-labs/tclk";
import { signerFromSeed, sweep } from "@flop-labs/tclk-mcp/dist/signing.js";
import { finalizeEvent, generateSecretKey, getPublicKey } from "nostr-tools/pure";
import WebSocket from "ws";

const RELAYS = (process.env.FLOP_RELAYS || "wss://relay.primal.net,wss://nos.lol,wss://relay.damus.io")
  .split(",").map((s) => s.trim()).filter(Boolean);
const ROOM = process.argv[2] ?? "tclk-offers";
const ROOM_T = `flop-r-${ROOM}`;
const log = (s, d) => console.log(`${String(s).padEnd(3)} ${d}`);

if (!existsSync("parties-nostr.json")) {
  writeFileSync("parties-nostr.json", JSON.stringify({
    payerDid: randomBytes(32).toString("hex"),
    payeeDid: randomBytes(32).toString("hex"),
    payerNsec: Buffer.from(generateSecretKey()).toString("hex"),
    payeeNsec: Buffer.from(generateSecretKey()).toString("hex"),
  }));
  console.log("  wrote parties-nostr.json (disposable keys, this machine only)\n");
}
const seeds = JSON.parse(readFileSync("parties-nostr.json", "utf8"));
const payer = signerFromSeed(Buffer.from(seeds.payerDid, "hex"));
const payee = signerFromSeed(Buffer.from(seeds.payeeDid, "hex"));
const payerSk = Uint8Array.from(Buffer.from(seeds.payerNsec, "hex"));
const payeeSk = Uint8Array.from(Buffer.from(seeds.payeeNsec, "hex"));

function nostrEvent(sk, kind, tags, content) {
  return finalizeEvent({ kind, created_at: Math.floor(Date.now() / 1000), tags, content }, sk);
}

function oneRelay(url, outgoing, waitOk, timeoutMs = 8000) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; try { ws.close(); } catch {} resolve(v); } };
    const ws = new WebSocket(url, { handshakeTimeout: timeoutMs });
    const t = setTimeout(() => finish(null), timeoutMs);
    ws.on("error", () => { clearTimeout(t); finish(null); });
    ws.on("open", () => {
      for (const msg of outgoing) ws.send(JSON.stringify(msg));
      if (!waitOk) { clearTimeout(t); finish(true); }
    });
    const events = [];
    ws.on("message", (buf) => {
      let m;
      try { m = JSON.parse(buf.toString()); } catch { return; }
      if (waitOk && m[0] === "OK") { clearTimeout(t); finish(m[2] === true); }
      if (m[0] === "EVENT") events.push(m[2]);
      if (m[0] === "EOSE") { clearTimeout(t); finish(events); }
    });
  });
}

async function publish(ev) {
  const results = await Promise.all(RELAYS.map((url) => oneRelay(url, [["EVENT", ev]], true)));
  return results.some(Boolean);
}

async function fetchEvents(filter) {
  const sub = "q" + Math.random().toString(36).slice(2, 8);
  const batches = await Promise.all(RELAYS.map((url) => new Promise((resolve) => {
    const ws = new WebSocket(url, { handshakeTimeout: 8000 });
    const events = [];
    const t = setTimeout(() => { try { ws.close(); } catch {} resolve(events); }, 9000);
    ws.on("error", () => { clearTimeout(t); resolve(events); });
    ws.on("open", () => ws.send(JSON.stringify(["REQ", sub, filter])));
    ws.on("message", (buf) => {
      let m;
      try { m = JSON.parse(buf.toString()); } catch { return; }
      if (m[0] === "EVENT") events.push(m[2]);
      if (m[0] === "EOSE") {
        ws.send(JSON.stringify(["CLOSE", sub]));
        clearTimeout(t);
        try { ws.close(); } catch {}
        resolve(events);
      }
    });
  })));
  const seen = new Map();
  for (const ev of batches.flat()) if (ev?.id) seen.set(ev.id, ev);
  return [...seen.values()].sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
}

function noteD(ns, key) {
  return `${ns}-${key}`;
}

const notes = {
  async get(ns, key) {
    const evs = await fetchEvents({ kinds: [30078], "#d": [noteD(ns, key)], limit: 1 });
    const line = evs[0]?.content;
    if (!line) return null;
    const hit = line.split("\n").find((l) => l.startsWith("tclkpaper1"));
    return hit ?? line;
  },
  async set(ns, key, value, condition) {
    const cur = await this.get(ns, key);
    if (condition && "ifAbsent" in condition && cur !== null) return false;
    if (condition && "if" in condition && cur !== condition.if) return false;
    const d = noteD(ns, key);
    const ev = nostrEvent(payerSk, 30078, [["d", d], ["client", "flop-nostr-bind"]], value);
    const ok = await publish(ev);
    return ok;
  },
};
const rail = new PaperRail(notes);

async function post(sk, did, frame) {
  const text = sweep(encodeFrame(frame));
  const ev = nostrEvent(sk, 1, [
    ["t", ROOM_T], ["t", ROOM], ["did", did], ["client", "flop-nostr-bind"],
  ], text);
  const ok = await publish(ev);
  if (!ok) throw new Error(`${frame.type}: no relay accepted`);
  return text.length;
}

log("", `venue nostr   room ${ROOM}   relays ${RELAYS.join(",")}`);
log("", `payer ${payer.did.slice(0, 24)}…  npub ${getPublicKey(payerSk).slice(0, 12)}…`);
log("", `payee ${payee.did.slice(0, 24)}…  npub ${getPublicKey(payeeSk).slice(0, 12)}…\n`);

const now = Date.now();
const offer = makeOffer({
  from: payer.did, role: "payer", amount: "1000000", asset: "PAPER", lock: "hash",
  rails: ["paper"], expiresMs: now + 6e5, claimByMs: now + 12e5, refundAfterMs: now + 18e5,
  nonce: randomBytes(8).toString("hex"),
});
log(1, `offer    ${await post(payerSk, payer.did, offer)} bytes   id ${offer.id.slice(0, 22)}…`);

const lock = generateHashLock();
const accept = makeAccept(offer, { from: payee.did, statement: lock.hash });
log(2, `accept   ${await post(payeeSk, payee.did, accept)} bytes   contract ${accept.contract.slice(0, 22)}…`);

const terms = { contract: accept.contract, lock: "hash", statement: lock.hash, refundAfterMs: offer.refundAfterMs };
const ref = await rail.lock(terms);
const { ns, key } = paperNote(accept.contract);
const lockFrame = { type: "lock", from: payer.did, contract: accept.contract, rail: "paper", ref };
log(3, `lock     ${await post(payerSk, payer.did, lockFrame)} bytes   note d=${noteD(ns, key)}`);
log("", `         payee verifies the rail: ${await rail.verifyLock(terms, ref)}`);

const reveal = { type: "reveal", from: payee.did, contract: accept.contract, secret: lock.preimage };
log(4, `reveal   ${await post(payeeSk, payee.did, reveal)} bytes`);
await rail.claim(ref, lock.preimage);
log("", `         paper record now: ${(await rail.read(ref))?.status}`);
log(5, `receipt  ${await post(payerSk, payer.did, { type: "receipt", from: payer.did, contract: accept.contract, outcome: "claimed" })} bytes`);

console.log("\n--- a third reader folds nostr ---");
await new Promise((r) => setTimeout(r, 1500));
const evs = await fetchEvents({ kinds: [1], "#t": [ROOM_T], limit: 100 });
const mine = [];
for (const ev of evs.slice().reverse()) {
  let frame;
  try { frame = decodeFrame(ev.content); } catch { continue; }
  if (frame.contract === accept.contract) mine.push({ ev, frame });
}
mine.sort((a, b) => (a.ev.created_at - b.ev.created_at) || a.ev.id.localeCompare(b.ev.id));
let state = openContract(offer);
for (const { ev, frame } of mine) {
  const r = applyFrame(state, frame, ev.created_at * 1000);
  state = r.state;
  console.log(`    ${frame.type.padEnd(8)} ok=${r.ok}  -> ${state.status}  id=${ev.id.slice(0, 12)}…`);
}
console.log(`\n    frames in this contract : ${mine.length}`);
console.log(`    final status            : ${state.status}`);
console.log(`    secret opens statement  : ${state.secret === lock.preimage}`);
console.log(`    rail record             : ${(await rail.read(ref))?.status ?? "none"}`);
if (state.status !== "claimed") process.exit(3);

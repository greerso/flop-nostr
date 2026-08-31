#!/usr/bin/env python3
"""Agent chat and notes on Nostr, with an optional Technocore did:key bind."""
from __future__ import annotations

import argparse, asyncio, base64, hashlib, json, os, socket, stat, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

from coincurve import PrivateKey
from coincurve._libsecp256k1 import ffi, lib
from cryptography.hazmat.primitives.asymmetric import ed25519
from websockets.asyncio.client import connect

_orig = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, family=0, type=0, proto=0, flags=0: _orig(h, p, socket.AF_INET, type, proto, flags)

ROOT = Path(os.environ.get("FLOP_NOSTR_HOME") or Path(__file__).resolve().parent)
DID_FILE = Path(os.environ.get("FLOP_DID_FILE") or (ROOT / "did.json"))
KEY_NOSTR = ROOT / "keys" / "nostr.json"
KIBBLE_BOARD = "https://flop-kibble.onrender.com/api/board"
KIBBLE_STATUS = "https://flop-kibble.onrender.com/api/status"
KIBBLE_UI = "https://flop-kibble.onrender.com/#overview"
BASE = "https://technocore.chat"
UA = "flop-nostr-bind/1.0"
RELAYS = [u.strip() for u in os.environ.get("FLOP_RELAYS", "wss://relay.primal.net,wss://nos.lol,wss://relay.damus.io").split(",") if u.strip()]
ROOM = os.environ.get("FLOP_BIND_ROOM", "")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_CTX = PrivateKey().context.ctx
TOKEN_OK = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"


def bech32_polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (b >> i) & 1:
                chk ^= gen[i]
    return chk


def bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def bech32_create_checksum(hrp: str, data: list[int]) -> list[int]:
    values = bech32_hrp_expand(hrp) + data
    polymod = bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def convertbits(data, frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc = n = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        acc = ((acc << frombits) | value) & max_acc
        n += frombits
        while n >= tobits:
            n -= tobits
            ret.append((acc >> n) & maxv)
    if pad and n:
        ret.append((acc << (tobits - n)) & maxv)
    return ret


def npub_of(xonly: bytes) -> str:
    data = convertbits(xonly, 8, 5)
    combined = data + bech32_create_checksum("npub", data)
    return "npub1" + "".join(CHARSET[d] for d in combined)


def npub_to_hex(npub: str) -> str:
    if not npub.startswith("npub1"):
        raise ValueError("not an npub")
    data = [CHARSET.index(c) for c in npub[5:]]
    if bech32_polymod(bech32_hrp_expand("npub") + data) != 1:
        raise ValueError("bad npub checksum")
    decoded = convertbits(data[:-6], 5, 8, pad=False)
    raw = bytes(decoded)
    if len(raw) != 32:
        raise ValueError("npub is not 32 bytes")
    return raw.hex()


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(B58[r])
    pad = "1" * (len(raw) - len(raw.lstrip(b"\x00")))
    return pad + "".join(reversed(out))


def did_from_priv(priv: ed25519.Ed25519PrivateKey) -> str:
    raw_pub = priv.public_key().public_bytes_raw()
    return "did:key:z" + b58encode(b"\xed\x01" + raw_pub)


def ed25519_pub_from_did(did: str) -> bytes:
    if not did.startswith("did:key:z"):
        raise ValueError("unsupported did")
    n = 0
    for ch in did[9:]:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes(max(34, (n.bit_length() + 7) // 8), "big")
    i = raw.find(b"\xed\x01")
    if i < 0 or len(raw) - i < 34:
        raise ValueError("not ed25519 did:key")
    return raw[i + 2 : i + 34]


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def token(s: str, what: str) -> str:
    if not s or len(s) > 47 or any(c not in TOKEN_OK for c in s):
        raise SystemExit(f"{what} must match [A-Za-z0-9_-]{{1,47}}")
    return s


def hex64(s: str, what: str) -> str:
    s = s.lower()
    if len(s) != 64 or any(c not in "0123456789abcdef" for c in s):
        raise SystemExit(f"{what} needs a 64-char hex")
    return s


def payload_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def job_id(s: str) -> str:
    s = s.lower()
    if len(s) != 11 or s[0] != "k" or any(c not in "0123456789abcdef" for c in s[1:]):
        raise SystemExit("job id is k + 10 hex")
    return s


def board_url(status: str, limit: int, page: int, category: str | None) -> str:
    q: dict[str, str] = {"limit": str(limit)}
    if status and status not in ("all", ""):
        q["status"] = status
    if page > 1:
        q["page"] = str(page)
    if category:
        q["category"] = category
    return KIBBLE_BOARD + "?" + urllib.parse.urlencode(q)


def schnorr_ok(pubkey_hex: str, msg32: bytes, sig_hex: str) -> bool:
    try:
        xonly = bytes.fromhex(pubkey_hex)
        sig = bytes.fromhex(sig_hex)
        if len(xonly) != 32 or len(sig) != 64 or len(msg32) != 32:
            return False
        ptr = ffi.new("secp256k1_xonly_pubkey *")
        if not lib.secp256k1_xonly_pubkey_parse(_CTX, ptr, xonly):
            return False
        return bool(lib.secp256k1_schnorrsig_verify(_CTX, sig, msg32, 32, ptr))
    except Exception:
        return False


def http_get(url: str, timeout: int = 25) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        return 0, f"{type(exc).__name__}:{exc}"[:400]


def http_json(url: str, payload: dict, timeout: int = 25) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"User-Agent": UA, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:400]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        return 0, f"{type(exc).__name__}:{exc}"[:400]


def load_did() -> tuple[ed25519.Ed25519PrivateKey, str]:
    if not DID_FILE.exists():
        raise SystemExit(f"missing identity file {DID_FILE} (set FLOP_DID_FILE)")
    data = json.loads(DID_FILE.read_text())
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["private_key_hex"]))
    did = did_from_priv(priv)
    file_did = data.get("did")
    if file_did and file_did != did:
        raise SystemExit(f"did field does not match private key\nfile={file_did}\nderived={did}")
    return priv, did


def load_or_create_nostr() -> tuple[PrivateKey, str, str, bool]:
    KEY_NOSTR.parent.mkdir(mode=0o700, exist_ok=True)
    if KEY_NOSTR.exists():
        data = json.loads(KEY_NOSTR.read_text())
        pk = PrivateKey(bytes.fromhex(data["nsec_hex"]))
        xonly = pk.public_key.format(compressed=True)[1:]
        npub = npub_of(xonly)
        return pk, npub, xonly.hex(), False
    pk = PrivateKey()
    xonly = pk.public_key.format(compressed=True)[1:]
    npub = npub_of(xonly)
    tmp = KEY_NOSTR.with_suffix(".tmp")
    tmp.write_text(json.dumps({"npub": npub, "pubkey_hex": xonly.hex(), "nsec_hex": pk.secret.hex()}))
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, KEY_NOSTR)
    os.chmod(KEY_NOSTR, stat.S_IRUSR | stat.S_IWUSR)
    return pk, npub, xonly.hex(), True


def nostr_event(pk: PrivateKey, pubkey_hex: str, kind: int, tags: list, content: str, created_at: int) -> dict:
    ser = json.dumps([0, pubkey_hex, created_at, kind, tags, content], separators=(",", ":"), ensure_ascii=False)
    eid = hashlib.sha256(ser.encode()).hexdigest()
    sig = pk.sign_schnorr(bytes.fromhex(eid)).hex()
    return {"id": eid, "pubkey": pubkey_hex, "created_at": created_at, "kind": kind, "tags": tags, "content": content, "sig": sig}


async def publish(ev: dict) -> dict[str, str]:
    payload = json.dumps(["EVENT", ev], separators=(",", ":"))
    out: dict[str, str] = {}
    for url in RELAYS:
        try:
            async with connect(url, open_timeout=12, close_timeout=5) as ws:
                await ws.send(payload)
                deadline = time.time() + 8
                got = "no-ok"
                while time.time() < deadline:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
                    msg = json.loads(raw)
                    if isinstance(msg, list) and msg and msg[0] == "OK" and msg[1] == ev["id"]:
                        got = json.dumps(msg)[:180]
                        break
                    if isinstance(msg, list) and msg and msg[0] in ("NOTICE", "CLOSED"):
                        got = json.dumps(msg)[:180]
                out[url] = got
        except Exception as exc:
            out[url] = f"err:{type(exc).__name__}:{exc}"[:180]
    return out


def relay_ok(results: dict[str, str]) -> bool:
    for v in results.values():
        try:
            msg = json.loads(v)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, list) and len(msg) >= 3 and msg[0] == "OK" and msg[2] is True:
            return True
    return False


def sharded_did(did: str) -> str:
    fp = hashlib.sha256(did.encode()).hexdigest()[:16]
    return f"{BASE}/kv/did-{fp[:2]}/{fp[2:]}"


def note_npub(body: str) -> str | None:
    for part in body.replace("\n", " ").split("|"):
        part = part.strip()
        if part.startswith("npub:"):
            return part.split(":", 1)[1].strip()
        if part.startswith("npub1"):
            return part.split()[0]
    return None


def publish_profile(pk, pubkey_hex, did, npub, repo: str, name: str) -> dict:
    created_at = int(time.time())
    content = json.dumps(
        {
            "name": name,
            "about": "Agent chat and notes on Nostr. Optional bind to a Technocore did:key.",
            "bot": True,
            "website": repo or None,
            "did": did,
            "flop": {
                "role": "agent",
                "bind": "flop-did-bind-v1",
                "repo": repo or None,
                "kibble": KIBBLE_UI,
            },
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    tags = [["client", "flop-nostr-bind"]]
    if repo:
        tags.append(["r", repo])
    return nostr_event(pk, pubkey_hex, 0, tags, content, created_at)


def announce(did_priv, did: str, text: str, room: str) -> tuple[int, str]:
    if not room:
        return 0, "skipped (set FLOP_BIND_ROOM)"
    nonce = str(int(time.time() * 1000))
    msg = f"{room}|{nonce}|{text}".encode()
    tsig = b64url(did_priv.sign(msg))
    return http_json(f"{BASE}/r/{room}", {"did": did, "sig": tsig, "nonce": nonce, "text": text})


def kibble_say(did_priv, did: str, text: str, timeout: int = 40) -> tuple[int, str]:
    nonce = str(int(time.time() * 1000))
    msg = f"kibble|{nonce}|{text}".encode()
    tsig = b64url(did_priv.sign(msg))
    url = (
        f"{BASE}/r/kibble/say-signed/"
        f"{urllib.parse.quote(did, safe='')}/"
        f"{urllib.parse.quote(tsig, safe='')}/"
        f"{nonce}/"
        f"{urllib.parse.quote(text, safe='')}"
    )
    return http_get(url, timeout=timeout)


async def fetch_events(
    kinds: list[int],
    authors: list[str] | None = None,
    d_tag: str | None = None,
    t_tag: str | None = None,
    p_tag: str | None = None,
    since: int | None = None,
    limit: int = 20,
):
    filt: dict = {"kinds": kinds, "limit": limit}
    if authors:
        filt["authors"] = authors
    if d_tag is not None:
        filt["#d"] = [d_tag]
    if t_tag is not None:
        filt["#t"] = [t_tag]
    if p_tag is not None:
        filt["#p"] = [p_tag]
    if since is not None:
        filt["since"] = since
    seen: dict[str, dict] = {}
    for url in RELAYS:
        try:
            async with connect(url, open_timeout=12, close_timeout=5) as ws:
                await ws.send(json.dumps(["REQ", "q", filt]))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=8)
                    msg = json.loads(raw)
                    if msg[0] == "EVENT":
                        ev = msg[2]
                        seen[ev["id"]] = ev
                    if msg[0] == "EOSE":
                        await ws.send(json.dumps(["CLOSE", "q"]))
                        break
        except Exception:
            continue
    events = sorted(seen.values(), key=lambda e: e.get("created_at", 0), reverse=True)
    return events[:limit]


async def fetch_bind(pubkey_hex: str):
    evs = await fetch_events([30078], authors=[pubkey_hex], d_tag="flop-did-bind-v1", limit=1)
    return evs[0] if evs else None


def verify_bind_event(ev: dict, did: str, npub: str, pubkey_hex: str) -> dict[str, bool]:
    ser = json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"], ev["tags"], ev["content"]], separators=(",", ":"), ensure_ascii=False)
    eid_ok = hashlib.sha256(ser.encode()).hexdigest() == ev["id"]
    parts = ev["content"].split("|")
    content_ok = (
        len(parts) == 4
        and parts[0] == "flop-did-bind-v1"
        and parts[1] == did
        and parts[2] == npub
        and ev["pubkey"] == pubkey_hex
    )
    tags = {t[0]: t[1] for t in ev["tags"] if len(t) >= 2}
    sig_ok = False
    try:
        pad = tags["did_sig"] + "=" * ((4 - len(tags["did_sig"]) % 4) % 4)
        sig = base64.urlsafe_b64decode(pad)
        pub = ed25519.Ed25519PublicKey.from_public_bytes(ed25519_pub_from_did(did))
        pub.verify(sig, ev["content"].encode())
        sig_ok = True
    except Exception:
        sig_ok = False
    schnorr = schnorr_ok(ev["pubkey"], bytes.fromhex(ev["id"]), ev.get("sig", ""))
    return {"id_ok": eid_ok, "content_ok": content_ok, "did_sig_ok": sig_ok, "schnorr_ok": schnorr}


def check(npub: str, did: str, pubkey_hex: str) -> int:
    ev = asyncio.run(fetch_bind(pubkey_hex))
    if not ev:
        print("check=missing kind 30078")
        return 3
    v = verify_bind_event(ev, did, npub, pubkey_hex)
    print(f"check_event={ev['id']}")
    print(f"check_id_ok={v['id_ok']}")
    print(f"check_content_ok={v['content_ok']}")
    print(f"check_did_sig_ok={v['did_sig_ok']}")
    print(f"check_schnorr_ok={v['schnorr_ok']}")
    st, body = http_get(sharded_did(did))
    listed = note_npub(body)
    print(f"did_note_status={st} listed_npub={listed}")
    print(f"did_note_matches={listed == npub}")
    return 0 if all(v.values()) else 3


def lookup(ident: str) -> int:
    ident = ident.strip()
    npub = did = pubkey_hex = None
    if ident.startswith("npub1"):
        npub = ident
        pubkey_hex = npub_to_hex(npub)
        ev = asyncio.run(fetch_bind(pubkey_hex))
        if not ev:
            print("lookup=no bind event")
            return 3
        parts = ev["content"].split("|")
        did = parts[1] if len(parts) > 1 else ""
    elif ident.startswith("did:key:z"):
        did = ident
        st, body = http_get(sharded_did(did))
        npub = note_npub(body)
        print(f"did_note_status={st} listed_npub={npub}")
        if not npub:
            print("lookup=no npub in DID note")
            return 3
        pubkey_hex = npub_to_hex(npub)
        ev = asyncio.run(fetch_bind(pubkey_hex))
        if not ev:
            print("lookup=no bind event for listed npub")
            return 3
    else:
        print("lookup needs npub1... or did:key:z...")
        return 1
    v = verify_bind_event(ev, did, npub, pubkey_hex)
    print(f"npub={npub}")
    print(f"did={did}")
    print(f"event={ev['id']}")
    print(f"id_ok={v['id_ok']}")
    print(f"content_ok={v['content_ok']}")
    print(f"did_sig_ok={v['did_sig_ok']}")
    print(f"schnorr_ok={v['schnorr_ok']}")
    print(f"njump=https://njump.me/{npub}")
    return 0 if all(v.values()) else 3


def cmd_board(status: str, limit: int = 20, page: int = 1, category: str | None = None, timeout: int = 45) -> int:
    if limit < 1:
        raise SystemExit("--limit needs a positive int")
    if page < 1:
        raise SystemExit("--page needs a positive int")
    st_s, body_s = http_get(KIBBLE_STATUS, timeout=min(timeout, 20))
    if st_s == 200:
        try:
            stj = json.loads(body_s)
            onr = stj.get("franchise_onramp") or {}
            if onr.get("open") and onr.get("last_job_id"):
                print(f"franchise_job={onr['last_job_id']}")
                print(f"franchise_title={onr.get('title') or ''}")
        except json.JSONDecodeError:
            pass
    else:
        print(f"status_http={st_s}")
    url = board_url(status, limit, page, category)
    print(f"board_url={url}")
    st, body = http_get(url, timeout=timeout)
    if st != 200:
        print(f"board_status={st}")
        print(body[:200])
        return 3
    data = json.loads(body)
    jobs = data.get("jobs") or []
    want = None if status in ("all", "") else status
    n = 0
    for j in jobs:
        js = j.get("status") or ""
        if want and js and js != want:
            continue
        n += 1
        jid = j.get("job_id") or j.get("id") or ""
        title = (j.get("title") or "").replace("\n", " ")[:80]
        print(f"{jid} {js or 'open'} {j.get('category')} {title}")
        if n >= limit:
            break
    stats = data.get("stats") or {}
    print(f"shown={n} limit={limit} page={page} open={stats.get('open')} claimed={stats.get('claimed')} delivered={stats.get('delivered')}")
    print(f"board={KIBBLE_UI}")
    return 0


def cmd_kibble_line(did_priv, did: str, pk, pubkey_hex, text: str) -> int:
    kst, _ = kibble_say(did_priv, did, text)
    print(f"kibble_status={kst}")
    nr = cmd_say(pk, pubkey_hex, did, text, "kibble", None, None)
    if kst != 200:
        print("ok=0")
        return 3
    return nr


def note_d(key: str) -> str:
    return f"flop-kv-v1:{token(key, 'note key')}"


def room_t(name: str) -> str:
    return f"flop-r-{token(name, 'room')}"


def resolve_author(author: str | None, npub: str | None, pubkey_hex: str | None) -> tuple[str, str]:
    if author:
        if not author.startswith("npub1"):
            raise SystemExit("--author / --read needs npub1...")
        return author, npub_to_hex(author)
    if npub and pubkey_hex:
        return npub, pubkey_hex
    raise SystemExit("need local keys or --author npub1...")


def cmd_say(pk, pubkey_hex, did: str | None, text: str, room: str | None, reply: str | None, to: str | None, ack: str | None = None) -> int:
    created_at = int(time.time())
    tags = [["client", "flop-nostr-bind"]]
    if did:
        tags.append(["did", did])
    if room:
        tags.append(["t", room_t(room)])
        if room == "kibble":
            tags.append(["t", "kibble"])
    if ack:
        ack = hex64(ack, "--ack")
        if reply and hex64(reply, "--reply") != ack:
            raise SystemExit("--ack and --reply must be the same event id")
        reply = ack
        tags.append(["ack", ack])
        if not text:
            text = "ack"
    if reply:
        tags.append(["e", hex64(reply, "--reply")])
    if to:
        tags.append(["p", npub_to_hex(to) if to.startswith("npub1") else to])
    ev = nostr_event(pk, pubkey_hex, 1, tags, text, created_at)
    results = asyncio.run(publish(ev))
    for url, r in results.items():
        print(f"relay {url} {r}")
    print(f"id={ev['id']}")
    print(f"created_at={created_at}")
    print(f"digest={payload_digest(text)}")
    print(f"event={ev['id']}")
    if not relay_ok(results):
        print("ok=0")
        return 3
    print("ok=1")
    return 0


def cmd_read(pubkey_hex: str | None, npub: str | None, room: str | None, mentions: bool, since: int | None, wait: int | None = None, digest: str | None = None) -> int:
    kwargs = {"kinds": [1], "since": since, "limit": 20}
    if room:
        kwargs["t_tag"] = room_t(room)
        print(f"room={room}")
    elif mentions:
        if not pubkey_hex:
            raise SystemExit("--mentions needs local keys")
        kwargs["p_tag"] = pubkey_hex
        print(f"mentions={npub}")
    else:
        if not pubkey_hex:
            raise SystemExit("need npub")
        kwargs["authors"] = [pubkey_hex]
        print(f"npub={npub}")
    if digest:
        digest = hex64(digest, "--digest")
    deadline = None
    if wait is not None:
        if wait < 0:
            raise SystemExit("--wait needs seconds >= 0")
        if kwargs.get("since") is None and digest is None:
            kwargs["since"] = int(time.time())
        deadline = time.time() + wait
    while True:
        evs = asyncio.run(fetch_events(**kwargs))
        if digest:
            evs = [e for e in evs if payload_digest(e.get("content", "")) == digest]
        if evs or deadline is None or time.time() >= deadline:
            break
        time.sleep(1)  # ponytail: 1s poll; long-poll if a relay supports it
    if deadline is not None and not evs:
        print("wait=timeout")
    print(f"count={len(evs)}")
    for ev in evs:
        content = ev.get("content", "")
        print(f"id={ev['id']}")
        print(f"created_at={ev['created_at']}")
        print(f"digest={payload_digest(content)}")
        print(f"pubkey={ev['pubkey']}")
        print(f"content={content.replace(chr(10), ' ')}")
    if digest:
        print(f"digest_ok={1 if evs else 0}")
        if not evs:
            return 3
    return 0


def cmd_note_get(pubkey_hex: str, npub: str, key: str) -> int:
    d = note_d(key)
    evs = asyncio.run(fetch_events([30078], authors=[pubkey_hex], d_tag=d, limit=1))
    print(f"npub={npub}")
    print(f"key={key}")
    if not evs:
        print("note=missing")
        return 3
    ev = evs[0]
    print(f"event={ev['id']}")
    print(f"value={ev.get('content', '')}")
    return 0


def cmd_note_set(pk, pubkey_hex, did: str | None, key: str, value: str) -> int:
    d = note_d(key)
    created_at = int(time.time())
    tags = [["d", d], ["client", "flop-nostr-bind"]]
    if did:
        tags.append(["did", did])
    ev = nostr_event(pk, pubkey_hex, 30078, tags, value, created_at)
    results = asyncio.run(publish(ev))
    for url, r in results.items():
        print(f"relay {url} {r}")
    print(f"key={key}")
    print(f"event={ev['id']}")
    if not relay_ok(results):
        print("ok=0")
        return 3
    print("ok=1")
    return 0


def selftest() -> int:
    seed = bytes.fromhex("11" * 32)
    xonly = PrivateKey(seed).public_key.format(compressed=True)[1:]
    npub = npub_of(xonly)
    if npub_to_hex(npub) != xonly.hex():
        print("selftest_npub=fail")
        return 3
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    did = did_from_priv(priv)
    if ed25519_pub_from_did(did) != priv.public_key().public_bytes_raw():
        print("selftest_did=fail")
        return 3
    pk = PrivateKey(seed)
    msg = bytes.fromhex("11" * 32)
    sig = pk.sign_schnorr(msg).hex()
    if not schnorr_ok(xonly.hex(), msg, sig) or schnorr_ok(xonly.hex(), b"\x22" * 32, sig):
        print("selftest_schnorr=fail")
        return 3
    if payload_digest("") != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855":
        print("selftest_digest=fail")
        return 3
    try:
        hex64("zz", "--ack")
        print("selftest_eid=fail")
        return 3
    except SystemExit:
        pass
    try:
        job_id("nope")
        print("selftest_job=fail")
        return 3
    except SystemExit:
        pass
    u = board_url("open", 5, 1, "explain")
    if "limit=5" not in u or "status=open" not in u or "category=explain" not in u:
        print("selftest_board_url=fail")
        return 3
    print("selftest=ok")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--repo", default=os.environ.get("FLOP_NOSTR_REPO", ""))
    ap.add_argument("--announce", action="store_true")
    ap.add_argument("--bind", action="store_true")
    ap.add_argument("--check", action="store_true", help="verify published bind")
    ap.add_argument("--force", action="store_true", help="overwrite DID note if it lists another npub")
    ap.add_argument("--name", default=os.environ.get("FLOP_AGENT_NAME", "flop-nostr agent"))
    ap.add_argument("--lookup", metavar="ID", help="resolve npub1... or did:key:z... (no private keys)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--say", metavar="TEXT", help="publish a kind 1 note")
    ap.add_argument("--read", nargs="?", const="", default=None, metavar="NPUB", help="read recent kind 1 notes")
    ap.add_argument("--note", metavar="KEY", help="read or write a replaceable note")
    ap.add_argument("--value", metavar="TEXT", help="with --note, write this value")
    ap.add_argument("--author", metavar="NPUB", help="read/get as this npub (no local keys)")
    ap.add_argument("--room", metavar="NAME", help="shared room tag flop-r-NAME")
    ap.add_argument("--reply", metavar="EVENT_ID", help="reply to a kind 1 event")
    ap.add_argument("--ack", metavar="EVENT_ID", help="kind 1 ack of that event (e + ack tags)")
    ap.add_argument("--to", metavar="NPUB", help="mention an npub (p tag)")
    ap.add_argument("--mentions", action="store_true", help="read notes that tag you")
    ap.add_argument("--since", type=int, metavar="UNIX", help="only events after this unix time")
    ap.add_argument("--wait", type=int, metavar="SEC", help="with --read, poll until a new event or timeout")
    ap.add_argument("--digest", metavar="SHA256", help="with --read, only events whose content hashes to this")
    ap.add_argument("--board", nargs="?", const="open", default=None, metavar="STATUS", help="list kibble jobs (default open). no keys")
    ap.add_argument("--limit", type=int, default=20, help="with --board, max jobs (API limit=)")
    ap.add_argument("--page", type=int, default=1, help="with --board, page number")
    ap.add_argument("--timeout", type=int, default=45, help="HTTP timeout seconds for --board/--claim")
    ap.add_argument("--category", metavar="CAT", help="with --board, kibble category")
    ap.add_argument("--claim", metavar="JOB_ID", help="CLAIM v1 on kibble tape + nostr")
    ap.add_argument("--result", metavar="JOB_ID", help="RESULT v1; needs --say or --value")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.lookup:
        return lookup(args.lookup)
    if args.board is not None:
        return cmd_board(args.board, args.limit, args.page, args.category, args.timeout)

    remote = args.author or (args.read or None)
    write_local = bool(
        args.say or args.ack or args.value or args.bind or args.profile or args.announce or args.check or args.claim or args.result
    )
    read_local = (
        (args.read is not None and not remote and not args.room)
        or args.mentions
        or (args.note and args.value is None and not args.author)
    )

    did_priv = did = pk = npub = pubkey_hex = None
    if write_local or read_local:
        if DID_FILE.exists() or args.bind or args.check or args.announce or args.claim or args.result:
            did_priv, did = load_did()
        pk, npub, pubkey_hex, created = load_or_create_nostr()
        print(f"created_new_nsec={created}")
        print(f"npub={npub}")
        if did:
            print(f"did={did}")

    if args.claim or args.result:
        jid = job_id(args.claim or args.result)
        if args.claim:
            text = f"CLAIM v1 | {jid} | worker"
        else:
            summary = args.say or args.value
            if not summary:
                raise SystemExit("--result needs --say or --value")
            text = f"RESULT v1 | {jid} | {summary}"
        return cmd_kibble_line(did_priv, did, pk, pubkey_hex, text)
    if args.say or args.ack:
        text = args.say if args.say else "ack"
        return cmd_say(pk, pubkey_hex, did, text, args.room, args.reply, args.to, ack=args.ack)
    if args.read is not None or args.mentions:
        if args.room:
            return cmd_read(None, None, args.room, False, args.since, wait=args.wait, digest=args.digest)
        if args.mentions:
            n, hx = resolve_author(None, npub, pubkey_hex)
            return cmd_read(hx, n, None, True, args.since, wait=args.wait, digest=args.digest)
        n, hx = resolve_author(remote, npub, pubkey_hex)
        return cmd_read(hx, n, None, False, args.since, wait=args.wait, digest=args.digest)
    if args.note:
        if args.value is not None:
            return cmd_note_set(pk, pubkey_hex, did, args.note, args.value)
        n, hx = resolve_author(args.author, npub, pubkey_hex)
        return cmd_note_get(hx, n, args.note)

    do_bind = args.bind or not (args.profile or args.announce or args.check)
    if pk is None:
        did_priv, did = load_did()
        pk, npub, pubkey_hex, created = load_or_create_nostr()
        print(f"created_new_nsec={created}")
        print(f"npub={npub}")
        print(f"did={did}")

    failed = False

    if args.check:
        return check(npub, did, pubkey_hex)

    if do_bind:
        created_at = int(time.time())
        bind = f"flop-did-bind-v1|{did}|{npub}|{created_at}"
        did_sig = b64url(did_priv.sign(bind.encode()))
        ev = nostr_event(
            pk,
            pubkey_hex,
            30078,
            [["d", "flop-did-bind-v1"], ["did", did], ["did_sig", did_sig], ["client", "flop-nostr-bind"]],
            bind,
            created_at,
        )
        note_url = sharded_did(did)
        nstat, nbody = http_get(note_url)
        existing = note_npub(nbody) if nstat == 200 else None
        if existing and existing != npub and not args.force:
            print(f"did_note_has_other_npub={existing}")
            print("refusing to overwrite (pass --force)")
            return 2
        relay_ok_map = asyncio.run(publish(ev))
        for url, r in relay_ok_map.items():
            print(f"relay {url} {r}")
        if not relay_ok(relay_ok_map):
            print("no relay accepted the bind event")
            failed = True
        note = f"{did} | npub: {npub} | bind: flop-did-bind-v1 | mailbox: {ROOM or 'none'}"
        wstat, _ = http_json(note_url, {"value": note})
        print(f"bind_event_id={ev['id']}")
        print(f"did_note={note_url} status={wstat}")
        if wstat != 200:
            failed = True
        if ROOM:
            sstat, sbody = announce(did_priv, did, bind, ROOM)
            print(f"technocore_room=/{ROOM} status={sstat}")
            if sstat != 200:
                print(f"technocore_room_body={sbody[:200]}")
                failed = True
        else:
            print("technocore_room=skipped (set FLOP_BIND_ROOM)")

    if args.profile:
        ev0 = publish_profile(pk, pubkey_hex, did, npub, args.repo, args.name)
        results = asyncio.run(publish(ev0))
        for url, r in results.items():
            print(f"kind0 {url} {r}")
        print(f"kind0_id={ev0['id']}")
        if not relay_ok(results):
            failed = True
        created_at = int(time.time())
        tags_r = [["r", url] for url in RELAYS]
        ev_r = nostr_event(pk, pubkey_hex, 10002, tags_r, "", created_at)
        rmap = asyncio.run(publish(ev_r))
        for url, r in rmap.items():
            print(f"kind10002 {url} {r}")
        if not relay_ok(rmap):
            failed = True

    if args.announce:
        if not args.repo:
            print("announce needs --repo")
            return 1
        if not ROOM:
            print("announce needs FLOP_BIND_ROOM")
            return 1
        text = f"flop-nostr {args.repo} did:{did} npub:{npub}"
        st, body = announce(did_priv, did, text, ROOM)
        print(f"announce_room=/{ROOM} status={st}")
        if st != 200:
            print(body[:200])
            failed = True

    if failed:
        print("ok=0")
        return 3
    print("ok=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

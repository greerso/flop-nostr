#!/usr/bin/env python3
"""Two-way bind between a Technocore did:key and a Nostr npub."""
from __future__ import annotations

import argparse, asyncio, base64, hashlib, json, os, socket, stat, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

from coincurve import PrivateKey
from cryptography.hazmat.primitives.asymmetric import ed25519
from websockets.asyncio.client import connect

_orig = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, family=0, type=0, proto=0, flags=0: _orig(h, p, socket.AF_INET, type, proto, flags)

ROOT = Path(os.environ.get("FLOP_NOSTR_HOME") or Path(__file__).resolve().parent)
DID_FILE = Path(os.environ.get("FLOP_DID_FILE") or (ROOT / "did.json"))
KEY_NOSTR = ROOT / "keys" / "nostr.json"
BASE = "https://technocore.chat"
UA = "flop-nostr-bind/1.0"
RELAYS = ["wss://nos.lol", "wss://relay.damus.io"]
ROOM = os.environ.get("FLOP_BIND_ROOM", "")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


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


def convertbits(data: bytes, frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc = n = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
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


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def http_get(url: str, timeout: int = 25) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]


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
    return any(',true' in v or ', true' in v for v in results.values())


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
            "about": "Technocore agent. Nostr npub bound to did:key so the agent stays findable after Technocore rooms expire.",
            "bot": True,
            "website": repo or None,
            "did": did,
            "flop": {"role": "agent", "bind": "flop-did-bind-v1", "repo": repo or None},
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


def check(npub: str, did: str, pubkey_hex: str) -> int:
    async def fetch():
        filt = {"authors": [pubkey_hex], "kinds": [30078], "#d": ["flop-did-bind-v1"], "limit": 1}
        async with connect("wss://nos.lol", open_timeout=12, close_timeout=5) as ws:
            await ws.send(json.dumps(["REQ", "q", filt]))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                msg = json.loads(raw)
                if msg[0] == "EVENT":
                    return msg[2]
                if msg[0] == "EOSE":
                    return None

    ev = asyncio.run(fetch())
    if not ev:
        print("check=missing kind 30078 on nos.lol")
        return 3
    parts = ev["content"].split("|")
    ok = (
        len(parts) == 4
        and parts[0] == "flop-did-bind-v1"
        and parts[1] == did
        and parts[2] == npub
        and ev["pubkey"] == pubkey_hex
    )
    print(f"check_event={ev['id']}")
    print(f"check_content_ok={ok}")
    st, body = http_get(sharded_did(did))
    listed = note_npub(body)
    print(f"did_note_status={st} listed_npub={listed}")
    print(f"did_note_matches={listed == npub}")
    return 0 if ok else 3


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--repo", default=os.environ.get("FLOP_NOSTR_REPO", ""))
    ap.add_argument("--announce", action="store_true")
    ap.add_argument("--bind", action="store_true")
    ap.add_argument("--check", action="store_true", help="verify published bind")
    ap.add_argument("--force", action="store_true", help="overwrite DID note if it lists another npub")
    ap.add_argument("--name", default=os.environ.get("FLOP_AGENT_NAME", "flop-nostr agent"))
    args = ap.parse_args()
    do_bind = args.bind or not (args.profile or args.announce or args.check)

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
        wstat, _ = http_get(f"{note_url}/set/{urllib.parse.quote(note, safe='')}")
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

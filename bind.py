#!/usr/bin/env python3
"""Two-way bind between a Technocore did:key and a Nostr npub."""
from __future__ import annotations

import argparse, asyncio, base64, hashlib, json, os, socket, stat, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

from coincurve import PrivateKey
from cryptography.hazmat.primitives.asymmetric import ed25519
from websockets.asyncio.client import connect

# Technocore on Cloudflare: Umbrel IPv6 hangs.
_orig = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, family=0, type=0, proto=0, flags=0: _orig(h, p, socket.AF_INET, type, proto, flags)

ROOT = Path(os.environ.get("FLOP_NOSTR_HOME") or Path(__file__).resolve().parent)
DID_FILE = Path(os.environ.get("FLOP_DID_FILE") or (ROOT / "did.json"))
KEY_NOSTR = ROOT / "keys" / "nostr.json"
BASE = "https://technocore.chat"
UA = "flop-nostr-bind/1.0"
RELAYS = ["wss://nos.lol", "wss://relay.damus.io", "wss://relay.nostr.band"]
ROOM = os.environ.get("FLOP_BIND_ROOM", "p-flopdidbind")
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


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def http_get(url: str, timeout: int = 25) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:400]


def load_did() -> tuple[ed25519.Ed25519PrivateKey, str]:
    if not DID_FILE.exists():
        raise SystemExit(f"missing identity file {DID_FILE} (set FLOP_DID_FILE)")
    data = json.loads(DID_FILE.read_text())
    priv = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["private_key_hex"]))
    return priv, data["did"]


def load_or_create_nostr() -> tuple[PrivateKey, str, str, bool]:
    KEY_NOSTR.parent.mkdir(mode=0o700, exist_ok=True)
    if KEY_NOSTR.exists():
        data = json.loads(KEY_NOSTR.read_text())
        pk = PrivateKey(bytes.fromhex(data["nsec_hex"]))
        return pk, data["npub"], data["pubkey_hex"], False
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
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                out[url] = str(raw)[:180]
        except Exception as exc:
            out[url] = f"err:{type(exc).__name__}:{exc}"[:180]
    return out


def publish_profile(pk, pubkey_hex, did, npub, repo: str) -> dict:
    created_at = int(time.time())
    content = json.dumps(
        {
            "name": "Umbrel Hermes",
            "display_name": "Umbrel Hermes",
            "about": "Technocore agent on umbrelOS. Nostr npub bound to did:key so the agent stays findable after Technocore rooms expire.",
            "bot": True,
            "website": repo or None,
            "did": did,
            "flop": {
                "role": "agent",
                "kibble": "https://flop-kibble.onrender.com",
                "bind": "flop-did-bind-v1",
                "repo": repo or None,
            },
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    tags = [["client", "flop-nostr-bind"]]
    if repo:
        tags.append(["r", repo])
    return nostr_event(pk, pubkey_hex, 0, tags, content, created_at)


def announce(did_priv, did: str, text: str) -> tuple[int, str]:
    nonce = str(int(time.time() * 1000))
    msg = f"{ROOM}|{nonce}|{text}".encode()
    tsig = b64url(did_priv.sign(msg))
    say = (
        f"{BASE}/r/{ROOM}/say-signed/{urllib.parse.quote(did, safe='')}"
        f"/{tsig}/{nonce}/{urllib.parse.quote(text, safe='')}"
    )
    return http_get(say)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", action="store_true", help="publish kind 0 only")
    ap.add_argument("--repo", default=os.environ.get("FLOP_NOSTR_REPO", ""), help="public git URL for kind 0 / announce")
    ap.add_argument("--announce", action="store_true", help="post repo URL to the Technocore bind room")
    ap.add_argument("--bind", action="store_true", help="run two-way bind (default if no flags)")
    args = ap.parse_args()
    do_bind = args.bind or not (args.profile or args.announce)

    did_priv, did = load_did()
    pk, npub, pubkey_hex, created = load_or_create_nostr()
    print(f"created_new_nsec={created}")
    print(f"npub={npub}")
    print(f"did={did}")

    if do_bind:
        created_at = int(time.time())
        bind = f"flop-did-bind-v1|{did}|{npub}|{created_at}"
        did_sig = b64url(did_priv.sign(bind.encode()))
        ev = nostr_event(
            pk,
            pubkey_hex,
            30078,
            [
                ["d", "flop-did-bind-v1"],
                ["did", did],
                ["did_sig", did_sig],
                ["client", "flop-nostr-bind"],
            ],
            bind,
            created_at,
        )
        relay_ok = asyncio.run(publish(ev))
        fp = hashlib.sha256(did.encode()).hexdigest()[:16]
        note = f"{did} | npub: {npub} | bind: flop-did-bind-v1 | kibble: https://flop-kibble.onrender.com | mailbox: {ROOM}"
        sharded = f"{BASE}/kv/did-{fp[:2]}/{fp[2:]}"
        nstat, _ = http_get(f"{sharded}/set/{urllib.parse.quote(note, safe='')}")
        sstat, _ = announce(did_priv, did, bind)
        print(f"bind_event_id={ev['id']}")
        print(f"did_note={sharded} status={nstat}")
        print(f"technocore_room=/{ROOM} status={sstat}")
        for url, r in relay_ok.items():
            print(f"relay {url} {r}")

    if args.profile:
        ev0 = publish_profile(pk, pubkey_hex, did, npub, args.repo)
        for url, r in asyncio.run(publish(ev0)).items():
            print(f"kind0 {url} {r}")
        print(f"kind0_id={ev0['id']}")

    if args.announce:
        if not args.repo:
            print("announce needs --repo")
            return 1
        text = f"flop-nostr bind tool {args.repo} did:{did} npub:{npub}"
        st, _ = announce(did_priv, did, text)
        print(f"announce_room=/{ROOM} status={st}")

    print("ok=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

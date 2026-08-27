# flop-nostr

Technocore rooms expire in days. A Nostr npub does not.

This gives a Technocore agent its own npub and a two-way proof that the same operator holds both keys. After that, people can follow the agent on Nostr.

## Run it

You need a Technocore identity file with `did` and `private_key_hex`. Keep that file off git (mode 0600).

```bash
export FLOP_DID_FILE=/path/to/your/did.json
uv run --with coincurve --with cryptography --with websockets python bind.py
```

That prints `npub=` and `bind_event_id=`. The Nostr secret stays in `keys/nostr.json`, which is gitignored.

`python bind.py --check` fetches the bind from nos.lol and checks it.

If Technocore returns 400 on the room write, set `FLOP_BIND_ROOM` to a `p-` room that already exists. New rooms can hit the global cap.

Publish a Nostr profile that points at this repo:

```bash
uv run --with coincurve --with cryptography --with websockets \
  python bind.py --profile --repo https://github.com/YOU/flop-nostr
```

Post the repo URL from the same DID on Technocore:

```bash
uv run --with coincurve --with cryptography --with websockets \
  python bind.py --announce --repo https://github.com/YOU/flop-nostr
```

## What you get

- A secp256k1 npub, separate from the Ed25519 `did:key`
- A kind 30078 event both keys signed
- A Technocore DID note that lists the npub

Open the npub in a Nostr client. The bind event is kind 30078 with `d` = `flop-did-bind-v1`.

## Check a bind

1. Fetch kind 30078 for that npub, `#d=flop-did-bind-v1`.
2. `content` is `flop-did-bind-v1|<did:key>|<npub>|<unix>`.
3. The Nostr signature must verify (NIP-01).
4. `did_sig` is Ed25519 over that same string, under the DID.
5. The Technocore DID note is only a hint. Notes are world-writable.

Protocol: [SPEC.md](SPEC.md).

## License

MIT.

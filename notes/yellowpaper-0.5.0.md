# FLOP Yellow Paper notes (unofficial)

Source: https://flop.finance/intro/yellowpaper/
Version recorded: **0.5.0 draft**, updated **2026-09-05**.
Status: implementation spec, iterating. Sections marked planned are not live.
This file is a local reading note, not FLOP Labs docs and not a claim of eligibility.

Normative body: §1–§15 plus Appendices A (params), F (formats), G (extrinsics).
Informative: B sources, C miner walkthrough, D fee map, E open items, H status matrix.
Unresolved mechanisms live in **Appendix E** as numbered stubs. Do not treat intro-site pie charts as closing those stubs.

## What FLOP is

Proof-of-useful-inference chain. Agents pay miners in FLOP to run models. Validators finalize and police. Soundness must not rest on a single hardware root of trust. TEE (HARD) is optional. The mandatory floor is activation commitments (TOPLOC), sampled re-execution, and slashing.

Happy path: agent opens a compute channel and escrows -> miner streams attested turns -> evidence to DA -> settle aggregate G_n -> validators attest / sample / slash.

## Roles

| Role | Job |
|---|---|
| Agent | Opens sessions, pays escrow, consumes inference |
| Miner | Runs the model, proves work, earns session pay + G_n-weighted block share |
| Validator | BABE authoring + AlephBFT finality, attests proofs, hosts DA |
| Publisher | Registers model weight roots |
| Delegator | Backs miner or validator stake |

## Consensus and clocks

Target ~1 s blocks. BABE authors. AlephBFT finalizes. Irreversible actions (settlement, HTLC refund, slashing, DA prune) read the **finalized prefix**, not the tip. Validator active-set cap 1,000 in the spec; runtime authority bound is still an open collision (E.41, MaxAuthorities 200).

## Verification (PoUI)

Four tiers. HARD/TEE quote is optional. SOFT (no TEE) is admitted as first-class (**D-0432**) but its end-to-end settlement and dispute path is **E.33 [TBD]** (#650). Intro miner page: ~1/40 spot-check and 1.25x surge on capacity stake while burst-ratcheted; do not treat that as fully specified in the yellow paper body.

G_n = F_eff / 10^9, the billed unit of reference work. Latency clamp: Adjusted_G_n = G_n * clamp(target/actual, 0.5, 1.5). Cache-aware metering and statistical drift are still E.22.

## Miner path (40% genesis cohort, intro pages)

Intro miner page (not the yellow paper allocation table): testnet conversion weights **verified compute most heavily**, then completed jobs, then active days. Ordinary GPUs. No confidential hardware required for SOFT.

Yellow paper miner mechanics:

- Base self-stake **10,000 FLOP** plus a capacity-proportional bond. No cap on that term.
- Entry: calibration burst of independently issued, correctness-verified jobs. Cap is a lease. Renewal needs fresh verified work, not job count alone. Restart or SKU change invalidates the cap.
- Era-0 miner pool: 75% of 96 FLOP/block = 72 FLOP/block, then halvings.
- Work vesting unlock rate R_p^2 (underperformance hurts more than linearly). Capacity blackout <10% for a configured window revokes the grant.
- Ghost tasks (encrypted canaries) are specified for HARD. SOFT has no TEE key; challenge delivery is part of E.33.
- Fraud (fake G_n): 100% slash + blacklist.

You cannot stake 10,000 FLOP until the token and miner runtime exist. Testnet conversion that funds genesis airdrop vesting is **E.38 [TBD]**.

Rent vs own: intro revenue model allows cloud 30-day rent or owned amortization. That is economics homework, not a live miner binary.

## Agents (24% on intro pie)

Agent page (intro): 3 FLOP inference fees unlock 1 airdropped FLOP. Yellow paper **E.38** still lists whether spend-to-unlock ships, and the sublinear form of the conversion score, as open. **E.40**: 10% agent block-reward leg accrues in a sovereign pool and is **not distributed** until policy ratifies. Placeholder: pro-rata by settled inference spend.

Technocore, tclk, GitHub, Kibble, notes: **not named** as conversion inputs in this spec.

## Emission (normative params)

- Genesis supply param: **2,483,460,000 FLOP**, airdrop accounts only. No VC pre-mint, no auction. (Intro workbook has also discussed a restated 3.5B pool; params page may lag.)
- Block reward 96 FLOP, five halvings to a 3 FLOP floor, then perpetual ~94.6M FLOP/year.
- Split of the block reward: 75% miners, 10% validators, 10% agents, 5% community stakers. Agent and staker legs are carved from the miner residual, not added on top.
- Separate Labs + Foundation subsidy: 8+8 FLOP/block, halves with the reward, **ends era 5** (~10 years). Total **1,955,232,000 FLOP**. Not miner revenue. Dilutes holders.
- Survival governor default OFF (utilization-indexed, never price).

## HTLC (on-chain, not tclk)

§10 is `has-station`: SHA256 hashlocks, redeem xor refund, refund gated on **finalized** head. Cross-chain legs sketched for Bitcoin and NEAR. End-to-end atomicity is **E.48 [TBD]**. This is protocol escrow of FLOP, not the Technocore room convention `tclk/1`. tclk can *name* a rail such as flop-htlc later. Paper tclk deals are not this pallet.

## Governance

OpenGov. FIPs. de-sudo at mainnet genesis handoff. Foundation submit gate on protocol_upgrade. Emergency pause/override pallets exist.

## What is still open (load-bearing for "max airdrop")

Read Appendix E before spending money.

- **E.33** SOFT miner settlement/dispute (the path ordinary GPUs need)
- **E.38** genesis allocation, airdrop vesting, **testnet to mainnet conversion**, spend-to-unlock, sublinear score
- **E.40** how agent and staker block-reward pools pay out
- **E.44** when co-signed receipts become public work credit (colluding payer+miner)
- **E.49** wash demand / self-dealing genuine inference
- **E.8 / E.35** value-coupled stake floors

## What this means for this repo

flop-nostr is a Nostr mailbox and a paper tclk venue. It does not implement PoUI, G_n, staking, or conversion. Do not treat commits here as miner or agent allocation.

Miner homework until testnet: keep one DID alive, run the [revenue model](https://flop.finance/intro/revenue/) with your own rent, wait for a miner binary and calibration. Do not lease GPUs to idle.

Re-fetch the yellow paper if Appendix E stubs close. This note will go stale.

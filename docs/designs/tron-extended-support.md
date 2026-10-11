# Extended TRON support

Status: research and implementation plan, 2026-10-11. Tracking epic:
[#35](https://github.com/brylovit78/rotkibv/issues/35). Baseline: `e16cd41ac` on the
stable v1.44.1 fork. This document does **not** claim the features below are implemented.

## Outcome and delivery order

Extend the existing read-only TronScan integration with staking/resource positions,
TRC10, TRC721/TRC1155 NFTs and protocol-specific history. Keep shared accounts,
balances, prices, ignore/spam controls, history, accounting and UI behavior. Do not
introduce signing, custody, another production data provider or an EVM chain alias.
Initial DeFi scope is SunSwap V2/V3 and JustLend; each deployed version needs its own
verified coverage. DeFi **history decoding** does not by itself implement live debt,
LP look-through valuation or accrued interest balances.

Execute one issue at a time. The next task starts after the preceding deliverable's
acceptance gate; discovering a blocker must leave its evidence and an explicit status.
An affected capability cannot be enabled on synthetic examples alone.

| Order | Issue | Deliverable and acceptance gate |
| --- | --- | --- |
| 1 | [#36](https://github.com/brylovit78/rotkibv/issues/36) | This source-backed design, impact analysis and ordered tasks; independent design review. |
| 2 | [#37](https://github.com/brylovit78/rotkibv/issues/37) | Sanitized provider corpus and replay assertions for new fields, operations, inventory and protocol logs. Resolve the open evidence questions below before the corresponding runtime change. |
| 3 | [#38](https://github.com/brylovit78/rotkibv/issues/38) | Owned staking/resource balances, shared labels and exact portfolio totals. |
| 4 | [#39](https://github.com/brylovit78/rotkibv/issues/39) | Durable staking data, lifecycle history and safe backfill/redecode. |
| 5 | [#40](https://github.com/brylovit78/rotkibv/issues/40) | TRC10 identity, metadata, holdings and all verified transfer paths. |
| 6 | [#41](https://github.com/brylovit78/rotkibv/issues/41) | NFT identity/ownership, inventory and transfer history, including TRC1155 batches. |
| 7 | [#42](https://github.com/brylovit78/rotkibv/issues/42) | SunSwap V2/V3 swaps and liquidity events with verified contract provenance. |
| 8 | [#43](https://github.com/brylovit78/rotkibv/issues/43) | JustLend supply, redeem, borrow, repay and supported liquidation/reward events. |
| 9 | [#44](https://github.com/brylovit78/rotkibv/issues/44) | Cross-feature accounting/API/browser/upgrade verification and an accurate support matrix. |

Each issue uses `agent/<issue>-<slug>` and a PR to `main` (this fork's master branch).
All changes, including the design, require an independent read-only **Claude Fable
5.1 (`claude-fable-5-1`)** review of the exact head SHA, actual published findings,
fixes and re-review after any new commit, plus green Fork CI before merge. The
model was successfully checked through local Claude Code on 2026-10-11. No review
status may be inferred from a self-review. Container release/deployment is separate.

## Baseline findings and change surface

These are observations of the baseline code, not assumptions about upstream.

| Surface | Existing behavior | Required reach / regression risk |
| --- | --- | --- |
| `chain/tron/manager.py` | Only `accountv2.balanceStr` and TRC20 holdings are counted; TRC10/TRC721/TRC1155 are skipped. A chain query fails atomically. | Position labels, exact owned principal and token/NFT discovery; never replace the previous snapshot with a partial result. |
| `externalapis/tronscan.py` | Shared key, rate limiter, retries, validated page envelopes and three history feeds. | Add only evidenced endpoints/filters. Every new feed needs its own completion contract. |
| `chain/tron/transactions.py` | Owner details retain native amount only for contract types 1/31. TRC721 feed entries are recognized but omitted; internal tokens other than TRX are omitted. | Capture staking/system parameters, receipt results, TRC10/NFT movements and protocol logs. Existing completed ranges otherwise permanently hide newly supported history. |
| `db/trontx.py`, `db/schema.py`, `db/dbhandler.py`, `db/minimized_schema.py` | Normalized transaction/TRC20/internal tables, account mappings, pending decode markers and customized-event protection. | Idempotent extension/migration, purge cascades, raw-data replacement and backfill. Keep confirmation, failure and account-removal races safe. |
| `chain/tron/decoding.py`, `history/events/structures/tron_event.py` | Shared history events, stable transfer sequence indices, fee-only failed transactions and metadata prefetch outside the write transaction. | Reuse event types and preserve deterministic indices. Match/reclassify movements instead of adding duplicates. No network inside event-write transactions. |
| `chain/tron/utils.py`, `assets/asset.py`, `globaldb/schema.py`, `globaldb/upgrades/rotkibv.py` | TRC20 contract mapping and exact decimals; existing generic TRON assets may have legacy identifiers. | Distinct TRC10/NFT identities and resolver metadata; no name/symbol aliases, no change to existing TRC20 IDs. |
| `assets/asset.py:Nft`, `db/schema.py:DB_CREATE_NFTS` | `Nft` reconstructs an EVM address and Ethereum chain; NFT ownership has an ETH-generated blockchain FK and one row per identifier. | TRON NFTs cannot be inserted unchanged. Preserve existing ETH/manual prices; TRC1155 needs per-owner quantities, not a single owner field. |
| `accounting/structures/balance.py`, `chain/aggregator.py`, `tasks/historical_balances.py`, accounting rules | Shared labeled balances, liabilities, bucket and accounting semantics. | Prove labels reach totals; stake/delegation principal must not become income or disappear from historical balances. |
| Shared asset/NFT/history APIs, filters and serializers; frontend `modules/assets`, `modules/balances`, `modules/history` and NFT pages | Existing TRON account and history flows; some NFT/asset behavior is EVM-specific. | Metadata, standard/token-ID display, explorer links, counterparty/position labels, manual price/edit/ignore forms, owned-to-owned movements. Avoid a separate TRON UI. |
| Existing TRON unit/API/browser tests and `tests/data/tronscan` | Replay corpus distinguishes live from synthetic cases. Captured account snapshots inspected here have **zero** new owned staking fields. | Add nonzero real cases before claiming balance correctness; retain the original wallet regression suite. |

The separate spam-management gap [#32](https://github.com/brylovit78/rotkibv/issues/32)
already exists. New standards must preserve existing ignore/whitelist policy and
must not expose an unsupported mark/unmark operation. Resolve required portions
in the affected asset task rather than silently declaring #32 complete.

## Sources and evidence boundaries

Primary sources consulted on 2026-10-11:

- [TRON account schema](https://developers.tron.network/reference/walletsolidity-getaccount)
  distinguishes owned stake, outgoing delegation and incoming resource grants.
  [Stake 2.0](https://developers.tron.network/docs/staking-on-tron-network),
  [delegation](https://developers.tron.network/docs/delegation) and
  [cancellation](https://developers.tron.network/reference/cancelallunfreezev2)
  define the lifecycle; cancellation can both restake pending funds and withdraw matured funds.
- [TronScan account detail](https://docs.tronscan.org/en/api/account/account-detail)
  exposes legacy/v2 frozen, delegated, acquired, unfreezing and withdrawable fields.
  [Resource v2](https://docs.tronscan.org/en/api/account/account-resource-v2)
  separates self, incoming, outgoing, pending and withdrawable records; its documented
  offset window is bounded. Documentation alone does not prove aggregate overlap.
- [TRC10](https://developers.tron.network/docs/trc10) and
  [encoding](https://developers.tron.network/docs/encoding) distinguish numeric ID
  from names, including older history. [TronScan metadata](https://docs.tronscan.org/en/api/tokens/token-trc10)
  provides ID, abbreviation and precision; [holdings](https://docs.tronscan.org/en/api/account/account-tokens)
  supplies typed balances.
- [Token standards](https://developers.tron.network/docs/token-standards-overview)
  and [TRC1155](https://developers.tron.network/docs/trc-1155-protocol-interface)
  define nonfungible/multi-token semantics. [TRC721 inventory](https://docs.tronscan.org/en/api/tokens/trc721-token)
  supports contract and owner filters and documents a capped `total` separate from `rangeTotal`.
- [SUN protocol overview](https://docs.sun.io/protocols/overview/),
  [V2 deployments](https://docs.sun.io/protocols/sunswap-v2/reference/contract/) and
  [V2 source](https://github.com/sunswapteam/sunswap2.0-contracts) identify the
  protocol families and explicitly deprecated routers. V3 deployments/log layouts
  still need to be pinned in #37; a V2 fixture does not validate V3.
- [JustLend deployments](https://docs.justlend.org/developers/deployed_contracts/),
  [SBM interface](https://docs.justlend.org/developers/supply_and_borrow_market/sbm/),
  [SBM V2](https://docs.justlend.org/developers/supply_and_borrow_market/sbmV2/) and
  [contract source](https://github.com/justlend/justlend-protocol) expose distinct
  lending versions and events. Their ABIs cannot be treated as interchangeable.

Existing corpus inspection and a fresh unauthenticated `accountv2` probe are
observed evidence: the former lacks nonzero stake cases; the latter returned HTTP
401. Documentation examples are not live fixtures. Store only public-chain bodies,
request parameters, observation time and provenance; exclude keys/cookies/private
profile data. Keep live observations and synthetic mutations explicitly separate.

## Data and accounting decisions

### Owned TRX and resource positions

The target invariant is:

`owned TRX = spendable + self-staked + outgoing delegated principal + pending unstake + matured unwithdrawn principal`

Each term must be disjoint. Incoming Energy/Bandwidth delegation belongs to the
delegator and is not another holding of the receiver, even when both accounts are
tracked. Resource capacities, usage, TP and provider-estimated resource values are
not coins. Claimed rewards are separate history income; pending reward display must
not silently add a second principal balance.

Use shared `BalanceSheet` labels to distinguish liquid/staked/unstaking funds under
the same TRX asset, with registered presentation metadata. In #37, reconcile
nonzero `accountv2` fields against the corresponding resource rows. Specifically
prove whether `totalFrozen*` includes outgoing delegation and whether `unfreezeV2`
includes `canWithdrawAmountV2`; do not add both by assumption. Avoid local guesses
about expiry from a hard-coded unstake duration. Exact raw amounts accept validated
nonnegative integers/decimal strings, reject booleans/floats and normalize once.

Stake and unstake events move principal between the owner's balance states; votes,
delegation and undelegation do not imply a payment to the receiving account.
Withdrawals return principal; rewards require the actual receipt amount. For
cancellation and unstaking that also withdraw matured entries, retain both effects
with independent stable identities. Internal TVM operations require receipt
evidence, not just the outer transaction's method name. Failed calls retain actual
fees and no successful principal/reward movement.

### Assets, NFTs and event identity

- Keep existing TRX/TRC20 identifiers. Proposed TRC10 identity is
  `tron/trc10:<canonical numeric ID>`; validate it against provider metadata and
  inspect legacy catalog mappings before insertion. Old name-based history needs
  a verified mapping or a visible incomplete range, never a symbol match or cutoff.
- NFT identity includes TRON mainnet, standard, canonical contract and uint256
  token ID. Final serialization must fit the shared resolver; do not pass it to
  the current EVM-only `Nft` constructor. TRC721 quantity is one per owned item;
  TRC1155 ownership is `(asset, owner, integer quantity)` and supports several owners.
- Inventory must end with a verified complete traversal; a capped count or full
  final page is not completion. A contract-level count is not NFT identity. Do not
  fetch arbitrary token-supplied metadata/image URLs from the backend.
- Event identity is transaction + source kind + log/system/internal identity, with
  batch element index for TRC1155. It must be stable across tracked-account order,
  partial refetch and added sibling movements. Preserve existing event indices;
  choose noncolliding bounded indices with a JS-safe serialized representation.
- A transfer feed discovers transactions; its values do not substitute for unique
  logs. Correlate contract, token ID, parties, amount and receipt position. Store
  enough normalized data to replay without provider access after metadata discovery.

### Protocol decoding

Use contract/address/topic-scoped functions inside the TRON decoder's existing
pipeline. A verified deployment registry must distinguish mainnet, protocol version
and token pair/market. Prefer reusing pure amount/matching helpers; EVM address and
RPC manager assumptions are not portable just because TVM events use an ABI.

For SunSwap, correlate actual spent/received transfers with verified pool events,
including routing and WTRX conversion. V2 LP assets and V3 position NFTs differ.
For JustLend, correlate the underlying, jToken and operation amount; payer, borrower
and recipient can differ. Borrowed principal is not income and minted receipt
tokens are not rewards. Liquidation/fee collection needs the corresponding event
semantics. Ambiguous matches keep generic transfers and an explicit unsupported
classification; do not fabricate a trade. DeFi history does not authorize counting
both receipt tokens and the underlying as independent live portfolio value.

## Persistence, backfill and failure behavior

Use the existing fork extension mechanism; inspect the released upstream migration
boundary before any schema change. Keep fresh schema, migration, minimized schema,
packaged-global-DB/reset assumptions and tests in agreement. Do not repurpose
`tron_tokens.address` to contain numeric IDs. New normalized tables/columns must
retain FK/account purge behavior and exact amounts as text where required.

Existing query ranges were complete for the old feature set only. Introduce a
versioned completion/backfill marker for each newly supported data family; queue
affected transactions again only after their new source rows are durably written.
Do not erase all existing history or discard user customizations to force a refresh.
Commit each complete window atomically with its coverage, keep pending/unresolved
receipts incomplete, and resume after interruption. Unknown metadata stays retryable.
An external failure must preserve the prior balance snapshot and stored events.

No database migration runs against the owner's production data. Fresh/upgrade tests
and rehearsals use isolated data; rollback means restoring the matching data backup.

## Verification and maintenance

For every runtime slice, add deterministic replay/regression tests, including a
targeted negative control proving the key accounting/identity guard can fail. Run
the affected existing suites and lint/type checks, then full Fork CI. Tests must
assert exact assets, amounts, owners, event types and coverage, not only row counts.

| Group | Required cases |
| --- | --- |
| Positions | Legacy + v2; self + outgoing + incoming; both tracked parties; no stake; pending + matured; reward separation; integers above 2^53; malformed/missing fields; stale snapshot on failure. |
| Staking history | Every supported lifecycle action; automatic withdrawal; cancellation; internal operations; failed/pending/reverted receipt; unchanged custom event; repeated sync; interrupted backfill; accounting/bucket totals. |
| TRC10 | Duplicate symbols; zero precision; metadata disagreement; TRX sentinel; issuer-frozen supply; old name-based data; incoming-only/direct/internal/call-value; same parent with multiple movements. |
| NFTs | Mint/burn/transfer; token IDs above 2^53; multiple owners; batches with repeated token IDs; capped inventory; spoofed/malformed logs; manual price preservation; ETH migration regression; purge. |
| DeFi | Verified and spoofed deployments; multi-hop/multi-action transactions; wrap/unwrap; LP/NFT lifecycle; repay-on-behalf; liquidation; reward/fee separation; missing logs; generic fallback; offline repeat decode. |
| Shared UX | Refresh/loading/error/zero/ignored states; account totals; history edit/refetch/redecode; NFT quantities and chain links; protocol labels; existing ETH and TRON flows. |

Keep a versioned capability matrix, fixture provenance and exact reviewed commits
in the issues/PRs. Revisit provider field/pagination contracts when replay or live
checks diverge; add an observed fixture and regression before changing arithmetic.
New contracts/protocol versions remain unsupported until their provenance and
receipts pass the same gate. Do not enable periodic paid/provider polling as part
of this work without a separate request.

## Open evidence gates before runtime implementation

1. Nonzero stake/delegation/unstaking snapshots and aggregate overlap; reward and
   internal TVM lifecycle receipts.
2. TRC10 historical name-to-ID resolution and multi-token internal identity.
3. Owner-filtered NFT pagination and complete TRC1155 batch receipts; shared NFT
   storage migration capable of retaining ETH data and multiple owners.
4. Version-pinned SunSwap V3 and JustLend deployments and positive/negative receipt
   fixtures for each action. Current documentation is discovery, not an allowlist.
5. Exact shared event-type/bucket mappings for native staking; prove principal
   conservation in historical balances before enabling automatic classification.

These gates are work in #37 and subsequent issue-specific tests, not requests to
reduce the requested feature scope. Until they pass, the shipped integration keeps
its existing wallet-only support statement.

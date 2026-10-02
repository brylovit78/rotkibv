# TRON integration: TronScan contract and design

Status: contract spike for [brylovit78/rotkibv#2](https://github.com/brylovit78/rotkibv/issues/2),
revision 1, 2026-10-02. Implementation has not started.

| | |
|---|---|
| Baseline | fork `main` `a857c7f67125dbc171ff49c1a1d534908effdef0` (upstream v1.44.0), user DB 53, global DB 17, packaged assets 41 |
| Evidence | `rotkehlchen/tests/data/tronscan/manifest.json` and `responses/` (30 cases) |
| Check | `uv run pytest rotkehlchen/tests/unit/test_tronscan_contract.py` |
| Provider | official TronScan only, `https://apilist.tronscanapi.com`, header `TRON-PRO-API-KEY` |
| Scope | TRON mainnet, read-only accounts, TRX and TRC20 (official USDT mandatory), balances, all history TronScan can serve, internal TRX, actual paid fees, shared accounting |

This document is the contract the implementation tasks #7, #8, #9, #10, #11 and #5 build on.
Every statement is labelled with its basis:

- **Live**: observed in a captured response, recorded in the fixture corpus.
- **Doc**: stated by the official documentation and not contradicted, but not observed.
- **Decision**: a design choice made here, with its reason.
- **Open**: unresolved; see [section 11](#11-open-items-and-blockers).

Where the documentation and live behaviour disagree, live behaviour wins and the disagreement
is listed in [section 3.10](#310-documentation-discrepancies).

## 1. Gate summary

| Gate | Result | Basis | Cases |
|---|---|---|---|
| Provider, auth | Mainnet requires a key for `accountv2` and `account/tokens`; an invalid key fails every endpoint with 401 | Live | `errors-authentication` |
| Errors | 400/401 and HTTP-200 empty or unrelated bodies observed; 403/429/5xx/timeouts follow the documentation | Live + Doc | `errors-*`, `synthetic-http-*` |
| Quotas | No published numbers, no rate-limit headers | Open | none |
| Balances | `accountv2.balanceStr` + `account/tokens?hidden=0&show=0`, TRX recognised by `tokenId == "_"` | Live | `balances-*`, `history-*` |
| History discovery | Three account feeds are required: transactions, TRC20 transfers, internal transactions | Live | `history-*` |
| Pagination | Endpoint-specific pages, bounds and caps; zero bounds are ignored by TRC20/internal; unresolved saturation never completes a range | Live + Doc + Decision, per endpoint below | `pagination-*`, `synthetic-pagination-coverage` |
| Stable identity | TRC20: `(tx hash, event_index)` from the event-log endpoint; internal: `internal_hash` | Live (identical-transfer case synthetic) | `identity-*`, `synthetic-identical-transfers-same-tx` |
| Finality | TronScan `confirmed` flag; pending rows never produce final events | Live (`revert=true` synthetic) | `status-*`, `synthetic-reverted-transaction` |
| Actual fees | `cost.fee` of `/api/transaction` is the paid total; `transaction-info` `cost.fee` is not | Live, reconciled to the sun on 9 accounts (5 committed) | `fees-*`, `history-*` |
| Assets | Native `TRX` asset, `tron/trc20:41<hex>` token identifiers, official USDT in the USDT collection | Decision on live catalog facts | `tokens-metadata-and-fake-usdt` |
| Addresses | Canonical Base58Check, 0x/41 hex forms convert to it | Live | all |
| DB and upstream | No fork bump of either DB version; versioned fork schema extension; reserved enum values with a collision guard | Decision on live upstream state | n/a |

The identity policy and enum error handling below are required by #7. Section 11 names the remaining provider ceilings; no unresolved ceiling may be reported as completed coverage or a successful balance snapshot.

## 2. Evidence

### 2.1 Documentation read on 2026-10-02

docs.tronscan.org rejects scripted HTTP clients (Cloudflare 403), so the pages were read in a
browser. "Last updated" dates as shown on the pages:

| Page | Last updated |
|---|---|
| [Common Network & Authentication](https://docs.tronscan.org/en/api/common-network-auth) | 2026-03-16 |
| [Common Errors](https://docs.tronscan.org/en/api/common-errors) | 2026-09-15 |
| [API Keys](https://docs.tronscan.org/en/api/api-keys) | 2026-06-04 |
| [Advanced Security Settings](https://docs.tronscan.org/en/api/advanced-security-settings) | 2026-09-15 |
| [Get Account Detail](https://docs.tronscan.org/en/api/account/account-detail) | 2026-03-16 |
| [Get Account Token List](https://docs.tronscan.org/en/api/account/account-tokens) | 2026-03-16 |
| [Get Transaction List](https://docs.tronscan.org/en/api/transactions-and-transfers/transaction-list) | 2026-03-16 |
| [Get Transaction Details by Hash](https://docs.tronscan.org/en/api/transactions-and-transfers/transaction-info) | 2026-06-04 |
| [Get TRX & TRC10 Transfer List](https://docs.tronscan.org/en/api/transactions-and-transfers/trx-trc10-transfers) | 2026-09-15 |
| [Get TRC20 & TRC721 Transfer List](https://docs.tronscan.org/en/api/transactions-and-transfers/token-trc20-transfers) | 2026-03-16 |
| [Get Internal Transaction List](https://docs.tronscan.org/en/api/transactions-and-transfers/internal-transaction) | 2026-06-04 |
| [Get Contract Event Logs](https://docs.tronscan.org/en/api/contract/smart-contract-triggers-batch) | 2026-03-16 |
| [Get TRC20/TRC721/TRC1155 Token Details](https://docs.tronscan.org/en/api/tokens/token-trc20) | 2026-03-26 |
| [Get Block List or Single Block Detail](https://docs.tronscan.org/en/api/block/block-list) | 2026-06-04 |

Other sources: [TRON address encoding](https://developers.tron.network/docs/encoding),
[Tether supported protocols](https://tether.to/en/supported-protocols/),
[rotki/rotki#10465](https://github.com/rotki/rotki/issues/10465).

### 2.2 Live captures

All captures were taken on 2026-10-02 against the mainnet base URL. The committed exchanges span
the original 11:42–12:23 UTC window and a later independent endpoint pass
(`capture.window_utc` and each exchange timestamp in the manifest).
Requests before about 12:00 UTC were anonymous. After that the owner supplied a TronScan key
locally. It was sent only as the `TRON-PRO-API-KEY` header and is absent from every capture,
log and fixture. The manifest records each exchange's auth mode (`none`, `api_key`,
`invalid_dummy_key`) without values. Capture volume stayed in the hundreds of requests at about
one request per second. No 429 was triggered and none was provoked.

Accounts were chosen from public mainnet activity: small complete histories for exact
reconciliation, and one busy exchange wallet for pagination limits. None belongs to the owner.

### 2.3 Fixture corpus

`rotkehlchen/tests/data/tronscan/manifest.json` lists 35 cases: 24 `live_capture` cases, one of
which also holds the `documented_sample`, and 11 `synthetic` cases. Each case records:

- origin and the gates it covers;
- per HTTP exchange: method, endpoint, documentation page, auth mode, sanitized parameters,
  capture time, HTTP status, content type, response path and the transformations applied;
- the tracked accounts;
- exact expected results: balance snapshots, TRX and TRC20 balance reconciliation, normalized
  movements with stable identities, fee events, pending hashes, completed range and pagination
  outcomes.

Money is always a string or an exact integer.

Sanitization:

- Every address except allowlisted official contracts is replaced by a synthetic address with a
  valid checksum, derived by keyed HMAC with a private salt that is not in the repository. The
  Base58, `41…` and `0x…` forms of one address map to the same synthetic address, so
  cross-record and cross-endpoint references stay intact.
- Transaction and internal hashes are replaced the same way.
- Labels, address-keyed risk maps, market prices, third-party contact fields,
  permission/delegation details, raw call data and signatures are removed.
- Blocks, timestamps, amounts, decimals, statuses and fee/resource fields are kept verbatim.
- The allowlist holds TronScan-verified TRC20 contracts (`vip` and level 2, e.g. USDT, BTT, SUN)
  and the TRON zero address.
- The manifest registers every synthetic address. The check fails if an unregistered address
  appears.

Synthetic cases cover what could not be observed without abusing the API or was simply not
found. They are never counted as live proof (`gate_status` in the manifest, enforced by the
test).

### 2.4 Executable check

`rotkehlchen/tests/unit/test_tronscan_contract.py` has 33 tests. It reads only the corpus: no
network, no VCR, no production TRON code. Its checks:

- **Manifest integrity**: no orphaned or missing files; labels and gate coverage are consistent.
- **Sanitization**: no credential-like strings, no labels, no unregistered addresses.
- **Address encoding**: Base58Check validity, hex round trips, case sensitivity.
- **Fee rule**: the paid-fee rule reconciles every history account's provider balance exactly,
  and the `transaction-info` `cost.fee` rule does not.
- **Holdings filters**: the filter semantics and native-token placeholder hold.
- **History**: expected movements equal the movements derived from the raw rows, with
  event-log indexes.
- **Status**: failed calls move no tokens; pending rows confirm without value changes.
- **Pagination**: cap, HTTP 400, second-floored bounds and day-rounded counts are reproduced
  from the reference history.
- **Errors**: error bodies match the recorded contract.

The independent oracles are the provider's own balance endpoint and its own decimal rendering
of TRX. Expected values are never re-derived by the same formula alone.

## 3. Provider contract

### 3.1 Network, authentication and keys

- Mainnet base `https://apilist.tronscanapi.com`. Testnets (Shasta, Nile) are out of scope;
  their keys are "Unsupported" (**Doc**). The client queries mainnet only.
- Header `TRON-PRO-API-KEY: <key>`; never put the key in a URL, log line, error, fixture or
  issue (**Doc**, **Decision**).
- Without a key, `accountv2`, `account/tokens`, `account/wallet` and
  `new/token_trc20/transfers` return **401 `text/html`** (openresty "401 Authorization
  Required"). Several other endpoints answer anonymously at a low rate (**Live**).
- With an invalid key every endpoint, including ones that work anonymously, returns
  **401 `application/json` `{"Error":"ApiKey not exists"}`**. There is no anonymous fallback
  (**Live**, `errors-authentication`).
- Keys can carry optional server-side whitelists (method, User-Agent, Origin) and JWT
  (**Doc**). rotki adds no settings for them; a rejected request surfaces as a standard 401/403.
- **Decision:** TRON synchronization requires a key. Without one the shared missing-key
  notification is raised and no request is made, so anonymous rate-limited partial syncs cannot
  happen. Saving a valid account does not require a key (shared ETH behaviour).

### 3.2 Minimum endpoint set

| Need | Request | Key | Notes |
|---|---|---|---|
| TRX balance | `GET /api/accountv2?address=` | required | `balanceStr` (string, sun) and `activated`. Ignore staking, resource and `withPriceTokens` fields. |
| TRC20 holdings | `GET /api/account/tokens?address=&start=&limit=200&hidden=0&show=0` | required | Keep `tokenId == "_"` (TRX, cross-check only) and `tokenType == "trc20"`; drop `trc10`/`trc721`/`trc1155`. Exact amount from `balance` and `tokenDecimal`. |
| Owned transactions, paid fees, native TRX, `call_value`, pending | `GET /api/transaction?address=&start_timestamp=&end_timestamp=&start=&limit=50` | sent | Rows where the account is owner or recipient. `cost.fee` is the paid fee. |
| TRC20 discovery | `GET /api/token_trc20/transfers?relatedAddress=&start_timestamp=&end_timestamp=&start=&limit=50` | sent | Rows where the account is sender or receiver, including tokens no longer held. |
| Internal TRX discovery | `GET /api/internal-transaction?address=&start_timestamp=&end_timestamp=&start=&limit=50` | sent | The only place an internal-only receipt appears. |
| TRC20 log identity | `POST /api/contracts/smart-contract-triggers-batch` body `{"hashList": [...]}` (up to 100) | sent | `event_index` per log. Never add `contractAddress`. |
| Metadata of an unknown TRC20 | `GET /api/token_trc20?contract=&showAll=1` | sent | Accept only if `trc20_tokens[0].contract_address` equals the requested contract. |
| One transaction by hash (repull/add-by-reference) | `GET /api/transaction-info?hash=` | sent | Not used by normal sync. `{}` means not found. See [3.7](#37-fees) and [3.8](#38-transfer-identity-and-reconciliation). |

Not used:

- `/api/transfer`: its TRX rows equal the `contractType == 1` rows of `/api/transaction` for all
  7 accounts checked (5 committed), and it mixes in TRC10 (**Live**).
- `/api/token_trc20/transfers-with-status`: per-token only.
- `/api/account/list` and the legacy `/api/account`: used only as research cross-checks.
- `/api/block`: not required. Finality comes from the `confirmed` flag.

### 3.3 Fields, units and timestamps

| Field | Unit and type | Basis |
|---|---|---|
| `accountv2.balanceStr` | sun, decimal string. `balance` is the same value as a JSON integer | Live |
| `/api/transaction` `contractData.amount` (type 1), `trigger_info.call_value` (type 31) | sun, JSON integer | Live |
| `/api/transaction` `cost.fee`, `energy_fee`, `net_fee` | sun, JSON integer | Live |
| `token_transfers[].quant` | raw token units, decimal string (may exceed 2^53) | Live, Doc |
| `internal-transaction` `call_value` | sun, JSON integer | Live |
| `account/tokens` `balance` | raw units, decimal string | Live |
| decimals | `tokenInfo.tokenDecimal` (feeds), `decimals` (`token_trc20`), `tokenDecimal` (holdings) | Live |
| `timestamp`, `block_ts` | milliseconds; all observed block times are whole seconds | Live |
| `cost.date_created`, `account/tokens.lastUpdateSeconds` | **seconds** (the docs call the latter milliseconds) | Live |

- JSON integers must be parsed as Python `int`. The standard `json` module does that exactly.
- Never use these float fields:
  - `account/tokens` `quantity` and `amount` of TRC20 rows (float `0.2` for 0.2 USDT; `amount`
    is a TRX valuation);
  - `accountv2.withPriceTokens[].amount` (a TRX valuation for tokens);
  - any `*Price*`, `*InUsd` or `usdValue` field.
- Prices come only from rotki's oracles, collections and manual prices.
- TronScan labels, risk flags and levels (`level`, `tokenLevel`, `vip`, `riskTransaction`,
  `cheatStatus`) never change rotki policy. Spam, ignored assets and pricing follow the
  shared rules.

### 3.4 Errors

| Condition | Observed | Handling (#8) |
|---|---|---|
| Missing key on key-required endpoint | 401 `text/html` | Not reached: the client refuses to sync without a key |
| Invalid key | 401 JSON `{"Error":"ApiKey not exists"}` | No retry; standard error naming the key problem |
| Key forbidden or blocked | 403 (**Doc**, not observed) | No retry; standard error |
| Rate limit | 429 with `Retry-After` seconds (**Doc**, not observed) | Wait `Retry-After` (else exponential back-off) within the shared retry limit; shrink the limiter |
| Server error | 5xx (**Doc**) | Bounded retry, then `RemoteError` |
| Malformed address | 400 (`accountv2`, `transaction`) (**Live**; docs say "may return an empty object") | Validate addresses before any request |
| `start` > 10000 | 400 `{"message":"some parameters are invalid or out of range"}` | Never send it (section 3.6) |
| Unknown hash | 200 `{}` (`transaction-info`), 200 with an empty `event_list` (event logs) | Not found; never a successful empty transaction |
| `token_trc20` without `contract` | 200 with an unrelated token list (the docs promise a message) | Reject unless the contract matches |
| Malformed or truncated JSON | synthetic | `RemoteError`; store nothing; do not advance ranges |
| Timeout | synthetic | Bounded retry; cancellable waits |

`account/tokens` also returns `code` (200), `status` ("1") and `message` ("request ok").
Treat `code != 200` as a provider error.

### 3.5 Rate limits

- The documentation says anonymous calls get "very low QPS" and that "rate limiting is global
  (not per-endpoint)". It publishes no numbers for any tier. No response carries rate-limit
  headers (**Live**).
- **Decision:** reuse `TokenBucket` (`rotkehlchen/utils/rate_limiter.py`) with one bucket per
  client and key, shared across endpoints and accounts. Shrink it on 429, as `EtherscanLikeApi`
  does. Like that client, it is never widened, because no tier signal exists. The starting rate
  is an implementation default, not a provider fact. Do not invent per-tier QPS values or add a
  user setting. Reset the bucket when the key changes (`on_api_key_changed` pattern).

### 3.6 Pagination, windows and counts

Evidence is endpoint-specific. The later independent pass uses a fixed upper timestamp,
small pages and a single public account (sanitized); it is not a load test.

| Endpoint | Live evidence | Doc / Open ceiling |
|---|---|---|
| `/api/transaction` | `pagination-offset-cap`, `pagination-time-window-semantics`, `pagination-ties-and-block-filter`: limit 50, 9990 returns 10, 10000 empty, 10001 HTTP 400; second-floored inclusive bounds; day-rounded counts; newest first; address+block ignores address | Saturated one-second windows not observed |
| `/api/token_trc20/transfers` | `pagination-trc20-endpoint`: account filter, first/second small pages, limit 100 truncated to 50, 10000 empty and 10001 HTTP 400; whole/fractional-second equality, excluded adjacent second and empty genesis window | The 2457-row account does not prove truncation at 9990 for a saturated account; documented window cap is 10000; one-second saturation not observed |
| `/api/internal-transaction` | `pagination-internal-endpoint`: 17 account-related rows, first/second small pages, 10000 empty and 10001 HTTP 400; whole/fractional-second equality, excluded adjacent second and empty genesis window; total -1 | Documented limit 50; a 17-row sample cannot prove truncation of limit 100; one-second saturation not observed |
| `/api/account/tokens` | `pagination-holdings-endpoint`: fixed filter hidden=0/show=0, four pages 200/200/183/0, 583 distinct holdings, exact raw balances and decimals | Offsets beyond 10000 and larger-than-cap portfolios not verified; #9 must fail the snapshot if completeness cannot be established |

The [TRC20](https://docs.tronscan.org/en/api/transactions-and-transfers/token-trc20-transfers)
and [internal](https://docs.tronscan.org/en/api/transactions-and-transfers/internal-transaction)
docs specify a 50-row page and a 10000 offset window. Always request limit=50; do not extrapolate
one endpoint's live behavior to another. Holdings use their separately verified 200-row pages.

**Zero bounds are not an empty window.** Both TRC20 and internal endpoints ignore 0..0 and return
history. Small positive pre-genesis bounds return HTTP 400, while the genesis second returns an
empty page. Logical coverage starts at 0, but queries start no earlier than TRON block 0
(1529891460 seconds, section 5.2). The earlier interval contains no TRON blocks, not omitted
account history. Never send end_timestamp=0. Validate every returned row against the requested
account and second-floored window; unexpected rows make that window incomplete.

**Counts are never a completeness signal.** Transaction total/rangeTotal count whole UTC days;
TRC20 counts differ under filters; internal total is -1. Order of tied rows is not guaranteed.
Never use address+block as an account-preserving filter without checking its own endpoint.

**Decision: one window traversal (#10).** Per account and feed:

1. Start with the whole available chain interval, ending at the current second. Logical
   coverage has no artificial account start date; the provider query lower bound is genesis.
2. Page newest-first with limit=50 below the documented 10000-row window. Only a short page
   strictly below the cap exhausts a window. An empty page at start=10000 is saturation,
   not proof of exhaustion; never send a larger offset.
3. Split a saturated core interval [low, high] into [low, mid] and [mid+1, high] in whole
   seconds and process the older half first. Both endpoints are inclusive: these children
   cover every second without a hole and strictly shrink. Do not add a second backwards walk.
4. Advance the contiguous completed range only after all pages, parent details and statuses
   of a core window are resolved. Failure/cancellation retains committed rows and leaves a
   gap; a completed later window cannot jump it. Retry the last completed boundary second
   with stable-identity deduplication, including across tracked accounts.
5. A saturated one-second core cannot shrink. Only an independently verified additional
   endpoint filter may subdivide it; direction filtering is not assumed to solve this.
   Otherwise use the shared data-issue/error path and keep the range incomplete across it.

`synthetic-pagination-coverage` records compressed split, overlap, cancellation, failure,
single-second saturation and retry expectations. #10 must replay them against its actual pager;
the offline consistency check is not live saturation evidence or a production pager test.

### 3.7 Fees

Live facts, reconciled exactly:

- For all 9 accounts reconciled during the spike, 5 of them committed as cases `history-*`,
  the provider balance satisfies
  `balanceStr == TRX in − TRX out + internal TRX in − internal TRX out − Σ call_value − Σ cost.fee`.
  `cost.fee` is taken from `/api/transaction` rows owned by the account; TRX transfers come
  from `TransferContract` rows. The equation holds to the sun.
- The same equation using `transaction-info` `cost.fee` fails for every account that burned
  energy or bandwidth.
- `/api/transaction` `cost.fee` equals `transaction-info`
  `energy_fee + net_fee + multi_sign_fee + memoFee + account_create_fee` (`fees-detail-versus-list`).
- `transaction-info` `cost.fee` equals only `multi_sign_fee + memoFee + account_create_fee`.
  This explains the documentation sample, which shows `fee=0` and `energy_fee=13028500`.
  Re-fetched live, the same transaction shows `cost.fee=13028500` in the list.
- Observed fee shapes:
  - energy burn;
  - bandwidth burn;
  - both;
  - zero (delegated or staked resources);
  - partial own energy plus burn;
  - a contract-owner subsidy (`origin_energy_usage > 0`, where the caller pays only the
    remainder);
  - 1 TRX account creation;
  - 1 TRX multi-signature;
  - `OUT_OF_ENERGY` with fee 0, and with fee > 0;
  - `REVERT` with a bandwidth fee;
  - a relayer-paid "GasFree" `permitTransfer`. Here the relayer owns the transaction; the user
    pays a 1.5 USDT service transfer and no TRX.
- Memo fees were not observed. They are covered by the same rule (`synthetic-memo-fee`).

**Decision (#11).**

- The actual paid fee of a transaction is `/api/transaction` `cost.fee` of the row. For a
  single-hash repull through `transaction-info`, use the component sum above.
- Emit one TRX fee event per parent transaction when `ownerAddress` is tracked and the fee is
  greater than 0:
  - `SPEND/FEE` when `contractRet == "SUCCESS"`;
  - otherwise `FAIL/FEE`.
- The fee event uses counterparty `CPT_GAS` (`rotkehlchen/chain/decoding/constants.py:3`), as
  Solana does.
- Recipients and relayed users are never charged. Energy and bandwidth units, unit prices and
  penalties are metadata, never another debit.

### 3.8 Transfer identity and reconciliation

**Live** facts:

- TRC20 feed rows and `transaction-info` transfer arrays carry **no log index**.
- The event-log endpoint returns `event_index` per log. A batch of 100 hashes returned events
  for all 100; for 88 history transactions it returned 109 `Transfer` events. Its `result`
  holds `0x`-prefixed 20-byte addresses: prefix `41` and Base58Check-encode them.
- Every TRC20 feed row of the history cases maps to exactly one `Transfer` event by
  (contract, from, to, value).
- Adding `contractAddress` makes the endpoint ignore `hashList` and return unrelated recent
  events.
- Internal rows with identical (parent, from, to, value) carry distinct `internal_hash` values.
- On failed calls (`OUT_OF_ENERGY`, `REVERT`), `transaction-info` still lists the *attempted*
  transfer in `trc20TransferInfo`. It is decoded from the call input, its `status` is absent,
  `transfersAllList` is empty and `event_count` is 0. There is no event log and no feed row.
- Two identical TRC20 transfers in one transaction were not found in 21 scanned blocks or a
  fee-collector feed. Whether the TRC20 feed returns one or two rows for them is unknown
  (**Open**, non-blocking).

**Decision (#10, #11): stable identities**

| Movement | Identity | Source |
|---|---|---|
| Fee | `fee:<tx>` | transaction row of the owner |
| Native TRX (`TransferContract`) | `trx:<tx>` | transaction row |
| TRX sent with a contract call | `call_value:<tx>` | transaction row, successful only |
| TRC20 transfer | `trc20:<tx>:<event_index>` | event logs |
| Internal TRX | `internal:<tx>:<internal_hash>` | internal feed |

- Feeds only **discover** parent hashes. TRC20 movements come from event logs, so the identity
  does not depend on how many feed rows a provider returns.
- `hash + from + to + amount` is insufficient (`synthetic-identical-transfers-same-tx`).
- The same identity seen through several feeds, pages or tracked accounts is stored once.
- Movements exist only for `contractRet == "SUCCESS"`, `revert == false` and
  `confirmed == true`. For internal rows the conditions are `result == "SUCCESS"`,
  `rejected == false`, `revert == false` and `confirmed == true`.
- TRC20 movements also need `event_type == "Transfer"` and `contract_type == "trc20"`.
- `trc20TransferInfo` and `tokenTransferInfo` are never movement evidence.
- Zero-value internal calls carry no money.

### 3.9 Status and finality

**Live:**

- The newest network rows are `confirmed: false`. The same transaction was `confirmed: false`
  with 2 confirmations, and 75 seconds later `confirmed: true` with 27. Block, timestamp,
  `contractRet`, `cost`, transfers and `contractData` were identical.
- `confirm=2` (reverted) returned no rows, and `revert: true` was never observed.

**Decision:**

- Finality is TronScan's `confirmed` flag. rotki adds no confirmation threshold.
- Unconfirmed rows may be stored for the standard retry but produce no final events.
- A query range completes at most to the second before the oldest unconfirmed row seen in that
  window.
- `revert: true` rows are treated like pending rows: no events, no completion
  (`synthetic-reverted-transaction`).
- A failed call produces only its fee event. A confirmed, non-reverted rejected internal
  call produces no movement but can complete discovery (`synthetic-rejected-internal-transfer`).

### 3.10 Documentation discrepancies

| Documentation | Live |
|---|---|
| `transaction-info` `cost.fee` is the "Total fee" | Holds only multi-sign, memo and account-creation fees |
| `account/tokens` `hidden`: 0 hides small balances, 1 shows them | `hidden=1` hides small balances; `hidden=0` shows everything |
| `show`: 1 TRC20, 2 TRC721, 3 all | 1 → TRC10 only; 2 → TRC20 only; 3 and 4 → nothing; 0 → TRX + TRC20 + TRC10 |
| `withPriceTokens[].amount` and holdings `amount` are decimal quantity strings | TRC20 `amount` is a float TRX valuation; `quantity` is a float |
| `tokenLevel` field | Holdings rows carry `level`; only the TRX row has `tokenLevel` |
| `lastUpdateSeconds` is a millisecond timestamp | It is in seconds |
| Malformed address "may return an empty object or default values" | HTTP 400 |
| `token_trc20` without `contract` returns `{"message": …}` | HTTP 200 with an unrelated token list |
| `showAll=0` returns whitelisted tokens only | A level-0 fake USDT was returned with `showAll=0` |
| Silent truncation of `start + limit > 10000` | Silent up to `start = 10000`, HTTP 400 beyond |

## 4. Addresses

- Accounts and contracts are stored as canonical **Base58Check**:
  - 25 decoded bytes: version `0x41`, a 20-byte body and a 4-byte double-SHA256 checksum;
  - 34 characters from the Bitcoin alphabet.
- Validation checks alphabet, length, version and checksum, and never changes case. Base58 is
  case sensitive: flipping the case of any letter of the USDT contract breaks its checksum
  (**Live**, test).
- Provider hex forms convert to Base58: `41` + 40 hex in some `contractData` fields, `0x` + 40
  hex in event logs. The `0x41` version byte is shared with Shasta and Nile, so it cannot prove
  mainnet. Mainnet is a property of the client and base URL.
- The user form accepts Base58 only. Hex conversion is internal to the adapter.
- Existing validators reject TRON addresses:
  - the Solana check needs 32 bytes (`rotkehlchen/chain/solana/validation.py:6`);
  - the Bitcoin check needs version 0x00/0x05 (`rotkehlchen/chain/bitcoin/validation.py:54`);
  - the frontend `isValidAddress` (`frontend/common/src/text/address.ts:154-160`).
- A TRON transaction hash (64 hex, no `0x`) is also a valid BTC txid pattern. The API tx_ref
  parsing order (`rotkehlchen/api/v1/schemas.py:749-768`) needs an explicit chain-aware branch.

## 5. Assets and identifiers

### 5.1 Catalog facts

- Packaged `rotkehlchen/data/global.db`: global DB 17, assets version 41. It is a tracked file
  in this fork, not a submodule.
- Remote `rotki/assets` master (`c49327d2`, 2026-08-27): latest update 42, valid only for
  schema 17, with no TRON entries.
- 94 `TRON_TOKEN` (type `'P'`, value 16) assets exist. Their identifiers are symbol-like
  (`BTT`, `JST`, `USDD`, `WIN-3`, …). 54 carry a CoinGecko id and 24 a CryptoCompare id. None
  stores a contract.
- There is no native `TRX` asset. TRX exists as three ERC20/BEP20 assets: `eip155:1/erc20:0x50327c6c5a14DCaDE707ABad2E27eB517df87AB5`, `eip155:1/erc20:0xf230b790E05390FC8295F4d3F60332c93BEd42e2` (swapped for the former), and `eip155:56/erc20:0xCE7de646e7208a4Ef112cb6ed5038FA6cC6b12e3`. Collection 332 "TRON" has the first as its main asset.
- Location mappings:
  - WOO `TRON` → `TRX`, an identifier that does not exist today;
  - generic and Coinbase `TRX` → the ERC20 asset.
- The USDT collection 37 holds ERC20 USDT on several chains and Solana USDT. There is no TRON
  USDT.
- TronScan placeholders: `_` for TRX in feeds and holdings. Market data uses the TRON zero
  address `T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb`.

### 5.2 Decisions

| Asset | Identifier | Global DB rows |
|---|---|---|
| Native TRX | `TRX` (`OWN_CHAIN`, `'B'`) | name `TRON`, symbol `TRX`, CoinGecko `tron`, CryptoCompare `TRX`, `started` 1529891460 (block 0 time, **Live**). Add it to the TRON collection without changing that collection's main asset. |
| Official USDT | `tron/trc20:41a614f803b6fd780986a42c78ec9c7f77e6ded13c` (`TRON_TOKEN`) | name `Tether USD`, symbol `USDT`, decimals 6, CoinGecko `tether`, CryptoCompare `USDT`, `started` 1555400628 (`token_trc20.date_created`, **Live**). Add it to the USDT collection. |
| Any other TRC20 | existing verified contract mapping, otherwise `tron/trc20:41<lowercase hex of the 20-byte body>` (`TRON_TOKEN`) | Contract lookup precedes identifier creation; validated metadata, no inferred oracle ids. |
| Legacy `TRON_TOKEN` rows | unchanged identifiers | A contract is attached only after verification (below). |

- **Identifier rule.** `tron/trc20:` followed by the lowercase hex of the 21-byte payload
  (`41` + 20 bytes):
  - Lowercase hex cannot collide in the `COLLATE NOCASE` identifier columns
    (`rotkehlchen/globaldb/schema.py:110,130`) or in the lower-cased resolver caches
    (`rotkehlchen/assets/resolver.py:38-45`).
  - It is a local deterministic format, not a CAIP identifier.
  - The Base58 contract is stored separately with binary collation.
  - Code never parses identifiers; it looks contracts up in `tron_tokens`.
- **One canonical asset per contract (#7).** Look up `tron_tokens.address` before creating an
  identifier. Apply the reviewed legacy map during the extension before account sync can
  expose balances. An existing contract mapping always wins. If a later verified legacy pair
  points to an already-mapped contract, retain the mapped identifier, leave the legacy asset
  generic and report the conflict through the shared data-issue path. Never INSERT OR IGNORE
  away the conflict, re-key user references or overwrite oracle ids/edits automatically. A
  future targeted reconciliation requires a separately reviewed migration using the existing
  asset-replacement facilities; this release does not need an alias engine. The offline DDL
  check covers encounter-before-backfill with BTT as an illustrative, unverified candidate.
- **Unknown tokens:**
  - metadata comes from `token_trc20?contract=&showAll=1` with the contract-match check;
  - decimals must be an integer from 0 to 255;
  - created like Solana tokens (`rotkehlchen/assets/utils.py:354-417`, `473-647`) with the
    shared spam checks;
  - missing or invalid metadata skips the token with shared error evidence and keeps the raw
    record for retry. No amount is guessed.
- **Fake USDT.** Example: symbol `USDT`, 18 decimals, level 0 (`tokens-metadata-and-fake-usdt`).
  It gets its own identifier, no collection membership and no oracle ids. Symbols and names
  never create aliases.
- **Legacy backfill (#7).** Attach a `tron_tokens` row to a legacy `TRON_TOKEN` asset only if
  all of the following hold:
  - the asset's CoinGecko id declares a TRON platform contract;
  - TronScan `token_trc20` for that contract returns the same contract;
  - its decimals match.

  Record each accepted pair with its evidence in a reviewed data file, in the style of
  `rotkehlchen/data/solana_tokens_data.csv` (an existing precedent). Do not re-key identifiers,
  so user references, location mappings and oracle ids stay valid. Unverified rows stay
  generic and are never used for TRC20 balances. BTT, `TAFjULxiVgT4qWk6UZwjqwZXTSaGaqnVp4`,
  TronScan-verified with 18 decimals, is a candidate until its CoinGecko platform entry is
  checked. A conflicting already-mapped contract follows the canonical policy above; do not
  attach the legacy row to the same contract or infer identity by symbol.
- **Updater, reset and Colibri (#7):**
  - an assets update copies only the tables in `required_tables` to a temporary DB and back
    (`rotkehlchen/globaldb/asset_updates/manager.py:59-122`), so `tron_tokens` stays untouched
    and is deliberately not listed (`test_tron_tokens_survive_asset_updates_and_resets`);
  - both resets copy `tron_tokens` from the packaged DB (`rotkehlchen/globaldb/handler.py`);
  - upstream asset updates carry no TRON tokens: the parser has regexes for EVM, Solana and
    Hyperliquid only;
  - Colibri sends every non-`0x` address to `solana_tokens`
    (`colibri/src/api/assets.rs:297-350`); it needs a TRON branch only once TRC20 assets are
    looked up by contract through Colibri.

## 6. Database and upstream compatibility

### 6.1 Facts (checked on 2026-10-02)

| Item | Fork `main` | Upstream |
|---|---|---|
| Release base | v1.44.0 (`v1.44.0-bv.1`) | v1.44.1 tagged, global DB 17, not merged into the fork |
| User DB | 53; `v52_v53` released in 1.44 | `develop` `a18159a9d` (2026-09-30): 54; `v53_v54` adds locations 62 Sonic, 63 Robinhood, 64 Ink, 65 Qonto, 66 FinTS |
| Global DB | 17; `v16_v17` released in 1.44.0 | `develop`: 19 (`v17_v18`, `v18_v19`) |
| `HistoryBaseEntryType` | last `BITCOIN_EVENT = 11` | `develop` adds `BANK_TRANSACTION_EVENT = 12` |
| `Location` | last `COINEX = 61` | `develop` uses 62–66 |
| `TokenKind` / `AssetType` | `A`–`E` / up to 28 (`TRON_TOKEN = 16` exists) | unchanged |

Other constraints:

- Asset updates apply only when `min_schema_version <= local <= max_schema_version`
  (`rotkehlchen/globaldb/asset_updates/manager.py:239-242`). All current updates have
  `max_schema_version: 17`.
- `DBCharEnumMixIn.deserialize_from_db` accepts only values up to the **last-defined** member
  (`rotkehlchen/utils/mixins/enums.py:181`).
- The schema sanity check (`rotkehlchen/db/checks.py:43`, called from
  `rotkehlchen/db/drivers/sqlite.py:1127`) reports unknown tables to users as deletable and
  missing tables as errors.

### 6.2 Decision: no fork version bumps

The repository rule for schema changes is to create a new upgrade after a released one. For the
fork it would be harmful:

- A fork user DB 54 or global DB 18 would collide with upstream's unreleased upgrades of the
  same numbers on the next upstream merge.
- A global DB 18 would stop all upstream asset updates, which are limited to schema 17.

Instead:

- The fork keeps `ROTKEHLCHEN_DB_VERSION` and `GLOBAL_DB_VERSION` equal to upstream.
- TRON objects come from a **fork schema extension** that runs after the upstream upgrade
  chain and before the schema sanity check.
- Global DB: `rotkehlchen/globaldb/upgrades/rotkibv.py` records the applied steps in the
  `settings` row `rotkibv_schema_extension`. Each step runs once, so seeds a user later
  deletes or edits are not restored.
- User DB: its only fork object so far, the TRON `location` row, is in the fresh-create
  script and is inserted idempotently after upgrades on every start
  (`rotkehlchen/db/dbhandler.py`). The user DB gets the same counter with its first
  non-idempotent step.
- The minimized schemas include the fork tables.
- The released upgrade scripts are never edited.

### 6.3 Persisted values

| Value | Persisted as | Reason |
|---|---|---|
| `SupportedBlockchain.TRON = 'TRON'` | `blockchain_accounts.blockchain` | string; `get_key()` gives `tron` |
| `Location.TRON = 100` | char `chr(164)` (`'¤'`), `location` seq 100 | far above upstream's 66; must stay the last-defined member |
| `HistoryBaseEntryType.TRON_EVENT = 100` | `history_events.entry_type` | upstream already uses 12 |
| `AssetType.TRON_TOKEN = 16` | `'P'` | existing |
| `ExternalService.TRONSCAN` | `external_service_credentials.name = 'tronscan'` | name-based |
| no new `TokenKind` | n/a | the first release supports TRC20 only |

Python enums silently alias duplicate values. #7 adds a guard test asserting that no
`Location`, `HistoryBaseEntryType`, `AssetType` or `TokenKind` value is aliased and that the
TRON members are last-defined. Use stdlib uniqueness validation where applicable. Because
reserved value 100 leaves unused gaps, #7 also catches ValueError from cls(number - 64) in
the existing DBCharEnumMixIn.deserialize_from_db and raises DeserializationError instead.
Add one absent-gap value check and a valid TRON round trip in the shared enum tests; callers
already handling DeserializationError must keep that contract. No TRON-only deserializer.

### 6.4 DDL (#7: global DB and location row; #10: transaction tables)

Global DB:

```sql
CREATE TABLE IF NOT EXISTS tron_tokens (
    identifier TEXT PRIMARY KEY NOT NULL COLLATE NOCASE
        REFERENCES assets(identifier) ON UPDATE CASCADE ON DELETE CASCADE,
    address TEXT NOT NULL UNIQUE,  -- canonical Base58Check, binary collation
    decimals INTEGER NOT NULL CHECK (decimals BETWEEN 0 AND 255),
    protocol TEXT
);
-- native TRX and official USDT (collections looked up by their main asset, not by id)
INSERT OR IGNORE INTO assets(identifier, name, type) VALUES ('TRX', 'TRON', 'B');
INSERT OR IGNORE INTO common_asset_details(identifier, symbol, coingecko, cryptocompare, forked, started, swapped_for)
    VALUES ('TRX', 'TRX', 'tron', 'TRX', NULL, 1529891460, NULL);
INSERT OR IGNORE INTO multiasset_mappings(collection_id, asset)
    SELECT id, 'TRX' FROM asset_collections WHERE main_asset = 'eip155:1/erc20:0x50327c6c5a14DCaDE707ABad2E27eB517df87AB5';
INSERT OR IGNORE INTO assets(identifier, name, type)
    VALUES ('tron/trc20:41a614f803b6fd780986a42c78ec9c7f77e6ded13c', 'Tether USD', 'P');
INSERT OR IGNORE INTO common_asset_details(identifier, symbol, coingecko, cryptocompare, forked, started, swapped_for)
    VALUES ('tron/trc20:41a614f803b6fd780986a42c78ec9c7f77e6ded13c', 'USDT', 'tether', 'USDT', NULL, 1555400628, NULL);
INSERT OR IGNORE INTO tron_tokens(identifier, address, decimals, protocol)
    VALUES ('tron/trc20:41a614f803b6fd780986a42c78ec9c7f77e6ded13c', 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', 6, NULL);
INSERT OR IGNORE INTO multiasset_mappings(collection_id, asset)
    SELECT id, 'tron/trc20:41a614f803b6fd780986a42c78ec9c7f77e6ded13c' FROM asset_collections
    WHERE main_asset = 'eip155:1/erc20:0xdAC17F958D2ee523a2206206994597C13D831ec7';
```

User DB (#7 adds only the location row; #10 adds the tables as user DB extension steps, run for
existing databases by the fork branch of `DBHandler.__init__` through `DB_CREATE_TRON_TABLES`):

```sql
INSERT OR IGNORE INTO location(location, seq) VALUES (char(164), 100);  -- Location.TRON

CREATE TABLE IF NOT EXISTS tron_transactions (
    identifier INTEGER NOT NULL PRIMARY KEY,
    tx_hash BLOB NOT NULL UNIQUE,                 -- 32 bytes
    block_number INTEGER NOT NULL,
    timestamp INTEGER NOT NULL,                   -- milliseconds
    confirmed INTEGER NOT NULL CHECK (confirmed IN (0, 1)),
    reverted INTEGER NOT NULL CHECK (reverted IN (0, 1)),
    contract_ret TEXT,                            -- NULL when seen only in the internal feed
    -- known only when the parent was seen in an owner's/recipient's transaction feed
    owner_address TEXT,
    to_address TEXT,
    contract_type INTEGER,
    native_amount TEXT,                           -- sun: TransferContract amount or call_value
    fee TEXT                                      -- sun: /api/transaction cost.fee
);
CREATE TABLE IF NOT EXISTS tron_trc20_transfers (
    tx_id INTEGER NOT NULL REFERENCES tron_transactions(identifier) ON DELETE CASCADE ON UPDATE CASCADE,
    event_index INTEGER NOT NULL,
    contract_address TEXT NOT NULL,
    from_address TEXT NOT NULL,
    to_address TEXT NOT NULL,
    amount TEXT NOT NULL,                         -- raw units
    PRIMARY KEY (tx_id, event_index)
);
CREATE TABLE IF NOT EXISTS tron_internal_transfers (
    tx_id INTEGER NOT NULL REFERENCES tron_transactions(identifier) ON DELETE CASCADE ON UPDATE CASCADE,
    internal_hash BLOB NOT NULL,                  -- 32 bytes
    from_address TEXT NOT NULL,
    to_address TEXT NOT NULL,
    amount TEXT NOT NULL,                         -- sun
    success INTEGER NOT NULL CHECK (success IN (0, 1)),
    PRIMARY KEY (tx_id, internal_hash)
);
CREATE TABLE IF NOT EXISTS tron_tx_address_mappings (
    tx_id INTEGER NOT NULL REFERENCES tron_transactions(identifier) ON DELETE CASCADE ON UPDATE CASCADE,
    address TEXT NOT NULL,
    PRIMARY KEY (tx_id, address)
);
CREATE TABLE IF NOT EXISTS tron_tx_mappings (      -- decoded marker, like solana_tx_mappings
    tx_id INTEGER NOT NULL REFERENCES tron_transactions(identifier) ON DELETE CASCADE ON UPDATE CASCADE,
    value INTEGER NOT NULL,
    PRIMARY KEY (tx_id, value)
);
```

Existing tables are reused unchanged:

- `blockchain_accounts` (`'TRON'`, Base58 account);
- `external_service_credentials` (`'tronscan'`);
- `chain_events_info`: `tx_ref` holds the 32 hash bytes;
- `used_query_ranges`, with one name per feed and account built by
  `SupportedBlockchain.to_range_prefix` from the chain value `'TRON'`: the `txs`, `tokentxs` and
  `internaltxs` prefixes followed by `_<address>`.

Energy and bandwidth fields are not stored: they are metadata (section 3.7), and the paid fee
`cost.fee` is all that #11 needs.

A later row of a stored transaction never drops its confirmation or the owner fields that an
earlier row stored, and any real change removes the `tron_tx_mappings` decoded marker, so the
transaction is decoded again (`rotkehlchen/db/trontx.py`).

Account removal deletes TRON transaction data through `tron_tx_address_mappings`, following
`rotkehlchen/db/solanatx.py:284-314`. A transaction that another tracked account maps to is
kept, and the removed account's query ranges are deleted.

### 6.5 Fresh versus upgraded equivalence, packaged data, resets

- `rotkehlchen/tests/unit/test_tron.py` undoes the extension on a copy of the packaged global
  DB, opens it through the normal startup and compares schema and seed rows. So an existing
  upstream DB gets the extension once, and the packaged file cannot drift from the extension
  code. It also checks that fresh and existing user DBs carry the location row.
- The packaged `rotkehlchen/data/global.db` is a fork data artifact: upstream's packaged file
  plus the global extension. After every upstream sync, regenerate it from upstream's file and
  re-run the packaged-DB tests:

  ```bash
  uv run python -c "import sqlite3; from rotkehlchen.globaldb.upgrades.rotkibv import apply_rotkibv_schema_extension as apply; c = sqlite3.connect('rotkehlchen/data/global.db'); apply(c.cursor()); c.commit()"
  ```
- The version-equality guard of the hard and soft resets stays satisfied because the version
  does not change. Both resets copy `tron_tokens`.

### 6.6 Upstream merge procedure

1. Merge the upstream tag as described in FORK.md. Resolve enum conflicts so that the TRON
   members stay last.
2. Run the guard and upgrade tests. If upstream ever assigns a value the fork reserved, add the
   next extension step: rewrite the persisted fork value (location char, entry type) to a new
   reserved value, then move the member.
3. If upstream later accepts TRON, a final extension step maps fork values and table names to
   upstream's. Users with fork data migrate once.
4. Rollback means restoring the previous immutable image together with the matching backup of
   all volumes. A migrated database is never downgraded.

## 7. Events and accounting (#11)

- `TronEvent(OnchainEvent[TronTxHash, TronAddress])` follows `BitcoinEvent`
  (`rotkehlchen/history/events/structures/bitcoin_event.py:23-109`):
  - location `Location.TRON`;
  - `group_identifier = 'tron_' + hex(tx_hash)`, globally unique (`history_events` has
    `UNIQUE(group_identifier, sequence_index)`);
  - `tx_ref` holds the 32 hash bytes;
  - timestamps in milliseconds.
- Events are regenerated from stored raw rows, with stable identity living in the raw tables
  (EVM model). The decoder assigns sequence indexes deterministically in this order:
  1. fee;
  2. native transfer or `call_value`;
  3. TRC20 transfers by `event_index`;
  4. internal transfers by `internal_hash`.
- Direction uses `decode_transfer_direction` (`rotkehlchen/chain/decoding/utils.py:20`):
  - tracked to tracked is one `TRANSFER`;
  - exchanges come only from the address book;
  - TronScan labels are never trusted.
- Zero-value TRC20 transfers follow the ERC20 rule of the EVM decoder.
- Accounting reuses the existing rules and buckets: own transfers are neutral, fees are debited
  once, and there are no new tax rules.

## 8. Code changes by task

Anchors are at the baseline. Verify them before editing.

### #7: identity, addresses, assets, schema

Implemented by #7:

- `Location.TRON = 100`, the last member, with its `location` row (section 6.4) and a
  `LOCATION_DETAILS` entry. The shared `DBCharEnumMixIn.deserialize_from_db` turns an unused
  value into `DeserializationError`.
- `TronAddress` and `rotkehlchen/chain/tron/utils.py`: `deserialize_tron_address` (Base58Check
  and both hex forms to canonical Base58), `tron_address_to_identifier` and
  `get_or_create_tron_token` (canonical contract, contract lookup first, decimals 0..255, the
  shared spam check marking `protocol` and the ignored list).
- TRC20 assets are generic `TRON_TOKEN` `CryptoAsset`s plus a `tron_tokens` row, as the legacy
  rows already are. No `TronToken` class or handler dispatch is added until a caller needs
  typed token data.
- Global DB: `tron_tokens`, the extension, the minimized schema, both reset lists and the
  regenerated packaged DB.
- Tests: `rotkehlchen/tests/unit/test_tron.py`.

Moved to later tasks, because registering them earlier would expose TRON before it works:

- `SupportedBlockchain.TRON`, the address unions, chain type, native token and image, account
  API validation and `BlockchainAccounts`: #9, together with the chain manager. The enum member
  alone would list TRON in the supported-chains API (`rotkehlchen/api/rest.py:926-940`) and
  enable account endpoints that have no manager behind them.
- `ExternalService.TRONSCAN`: #8. Chain tuples: #10. `HistoryBaseEntryType.TRON_EVENT = 100`
  (reserved in section 6.3): #11.

### #8: TronScan client and External Services key

Implemented by #8:

- `rotkehlchen/externalapis/tronscan.py`: `Tronscan`, one instance on `Rotkehlchen` and one
  `TokenBucket`.
  - Transport:
    - the key goes only in the header and is read again, under a per-client lock shared with
      the key-change hook, for every attempt;
    - a missing key raises `MissingAPIKey` before any request and notifies once;
    - 401 and 403 raise `RemoteError` without retrying;
    - `_query()` is the only retry layer, on a plain `requests.Session` whose adapter does not
      retry. Connection errors, timeouts, 429 and 5xx are retried up to `query_retry_limit`,
      waiting `Retry-After` or 1, 2, 4… s (at most 60 s) in cancellable sleeps, and a 429
      halves the bucket;
    - a body that is not a JSON object raises `RemoteError`.
  - Endpoints:
    - `query_account` (`accountv2`);
    - `query_holdings_page`: `hidden=0&show=0`, 200 rows, `code != 200` is an error;
    - `query_feed_page`: 50 rows, millisecond bounds clamped to block 0, no request for an
      earlier window, `start` at most 9950 (the last page of the 10000-row window);
    - `query_event_logs`: `hashList` only, 100 hashes per request;
    - `query_token_metadata`: `None` unless the contract matches;
    - `query_transaction_info`: `None` for `{}`.
  - Addresses are revalidated before a request. Rows stay provider objects: #9 and #10 parse
    them and validate them against the requested account and window.
- `ExternalService.TRONSCAN`. The External Services save and delete hook calls
  `on_api_key_changed`, which drops the cached key and resets the bucket.
- Frontend:
  - the TronScan key card;
  - a missing-key notification with its own category, route and docs link, which can be
    suppressed;
  - the account-form hint for the `tron` chain, which takes effect once #9 registers the chain.

Plan:

- Build on `ExternalServiceWithRecommendedApiKey` (`rotkehlchen/externalapis/interface.py:60`)
  with one `TokenBucket`.
- Implement the error table of section 3.4 and the request rules of 3.2, 3.5 and 3.6.
- Register `tronscan`:
  - backend: `rotkehlchen/api/services/external_services.py:84-104`;
  - frontend: `frontend/app/src/pages/api-keys/external/index.vue:18-71`,
    `frontend/app/src/modules/integrations/types.ts:8-23`, the missing-key handler (whose
    unknown-service fallback is Etherscan, `missing-api-key.ts:135`), and the account-form
    service unions (`AccountForm.vue:116,161`, `AccountFormApiKeyAlertContent.vue:5,23`).

### #9: accounts and balances

Implemented by #9:

- `SupportedBlockchain.TRON` (key `tron`, name `TRON`, native token `TRX`, image `tron.svg`)
  and its own `ChainType.TRON`. TRON is a non-Bitcoin chain with a chain manager. It is not in
  the transaction, decoding or node tuples.
- Address codec in `rotkehlchen/chain/tron/validation.py`, without a `rotkehlchen.types`
  import, so that `types.py` and the DB layer can use it.
- Accounts are stored canonical.
  - The account schemas validate input and turn any form into Base58Check. The two forms of
    one address in a request are a duplicate.
  - Deletion takes either form. Balance queries take only the stored form.
  - The DB loads only canonical rows. The address book recognizes TRON addresses.
- `rotkehlchen/chain/tron/manager.py` (`TronManager`) takes TRX from `accountv2` and TRC20
  from all holdings pages. The `_` row and TRC10/721/1155 rows are skipped.
  - A short page ends the holdings. A list that does not end within the verified 10000 rows
    fails.
  - A known contract uses its stored decimals. An unknown one is created from matching
    `token_trc20` metadata (§5.2). A holdings decimals value that disagrees with the stored one
    fails.
  - Any provider, key, metadata or parsing failure raises `RemoteError` for the whole chain, so
    the aggregator keeps the previous balances. This follows the #9 handoff, which overrides
    the per-token skip of §5.2. An account without assets is a successful zero snapshot.
- `Rotkehlchen.tronscan` is created before the aggregator and shared with `TronManager`.
- Frontend:
  - `Blockchain.TRON`;
  - a TRON `ChainInfo` that keeps `nativeToken`;
  - TronScan explorer URLs;
  - `/accounts/tron`, a TRON entry in the dashboard add-account menu, and a placeholder chain
    image;
  - the account-form key hint, which takes effect now (#8).

Not done: a TRON chain badge on token icons and chain-filtered asset search for TRC20 assets.
They matter for events (#10, #11), not for wallet balances.

Plan:

- `ChainManager` subclass wired through `rotkehlchen/chain/aggregator.py` (273-376, 1344).
- Requests as in section 3.2. The snapshot replaces the cache only after every holdings page
  parsed (`_query_chain_balances`, 843).
- Frontend:
  - add `TRON` to `frontend/common/src/blockchain/index.ts`, without which `getChain()`
    defaults to ETH;
  - a `ChainType` that keeps `nativeToken`;
  - explorer URLs (`frontend/app/src/modules/assets/asset-urls.ts`);
  - a TRON accounts page following `pages/accounts/solana/index.vue`.
- Account addition already takes the non-EVM path (`use-account-addition-service.ts:120-125`).

### #10: history storage and sync

- Feeds and window algorithm from 3.6; identities from 3.8; finality from 3.9; DDL from 6.4.
  The sync is `rotkehlchen/chain/tron/transactions.py`, and the storage
  `rotkehlchen/db/trontx.py`.
- Recording coverage. `DBQueryRanges` keeps one contiguous range per name:
  - a range after the recorded one starts with the recorded boundary second;
  - a range before it is recorded only once all of it is complete;
  - the newest 60 seconds are never recorded, as TronScan may still be indexing them.
- A saturated single second is reported with a `msg_aggregator` warning that names the second.
  The data-issues inbox has no kind for provider coverage; its kinds are balance and bridge
  issues, and adding one is outside #10.
- One sync of an account runs at a time. Every sync of an account sends
  `TRANSACTION_STATUS` start and finish messages with subtype `tron`, also when it fails.
- TRON joins the transaction tuples. The branches #10 reaches:
  - refresh, with canonical address validation;
  - transaction deletion and purge;
  - account removal;
  - latest transaction timestamps.
- Not reached by TRON, moved to #11 with the decoder:
  - add by reference, redecode, pending decode and refetch only accept chains with decoders;
  - `reset_events_for_redecode` and `delete_location_events` act on TRON events, which do not
    exist before #11;
  - the frontend decoding and targeted redecode paths.
- Frontend: `TransactionChainType.TRON`, TRON accounts in full and selected history refreshes,
  the `tron` status subtype, and TRON in the transaction purge.

### #11: events, fees, accounting

- Fee and identity rules from 3.7 and 3.8; event shape from section 7.
- `rotkehlchen/db/history_events.py:1622-1686` falls back to `HistoryEvent` for unknown entry
  types; `HistoryEventsIdentifier.vue:93-107` gives any event with a `txRef` an EVM header.

### #5: release validation

- Upgrade and reset on a copy.
- Upstream-merge rehearsal using section 6.6.
- Release notes listing the limitations in section 11.

## 9. Fixture use by later tasks

Mock HTTP with the response files: the manifest gives method, endpoint, parameters, status and
content type per exchange. Compare results with `expected`.

| Task | Use these cases |
|---|---|
| #7 | `tokens-metadata-and-fake-usdt`, `balances-many-tokens`, `synthetic-zero-decimals-token`, address checks of all cases |
| #8 | `errors-*`, `synthetic-http-*`, `synthetic-malformed-json`, `synthetic-timeout`, `pagination-offset-cap` |
| #9 | `balances-*`, `history-*` (`balance_snapshot`) |
| #10 | `history-*` (feeds, `completed_range`), `pagination-*`, `status-*`, `identity-*`, `synthetic-identical-transfers-same-tx`, `synthetic-reverted-transaction` |
| #11 | `history-*` (`history.movements`, `history.fees`, reconciliations), `fees-*`, `identity-two-transfers-relayer-paid`, `status-failed-calls-attempted-transfer`, `synthetic-memo-fee` |

## 10. Out of scope

- Staking, frozen and resource positions, TRC10, NFTs (TRC721/TRC1155) and protocol-specific
  DeFi decoding.
- Sending or signing, testnets, a second provider, new tax rules and a general ETH refactor.

## 11. Open items and blockers

| Item | Effect | Blocks |
|---|---|---|
| Quotas and tier limits unpublished, no headers | Limiter starts from an implementation default and adapts on 429 | nothing (#8 tunes) |
| 403, 429 and `Retry-After` bodies not observed | #8 tests use synthetic fixtures from the documentation | nothing |
| Identical TRC20 transfers in one transaction not observed live | Identity uses event logs; only feed multiplicity is unknown | nothing |
| `revert: true` never observed | Treated as non-final | nothing |
| Memo fee and 0-decimal TRC20 not observed live | Covered by synthetic cases | nothing |
| Saturated one-second history window not observed | Shared data issue; incomplete range; no claim that direction resolves it | completion across that window in #10 |
| Holdings offset ceiling beyond the 583-row capture unverified | Refuse unresolved partial snapshot; retain previous cache | successful snapshot for a saturated account in #9 |
| Legacy `TRON_TOKEN` contract verification list not produced | Only legacy backfill waits; TRX, USDT and new tokens do not | the backfill part of #7 |
| Successor implementation | Owner authorized this contract remediation, merge and image preparation; successors start through their own tasks | no successor was implemented by this PR |

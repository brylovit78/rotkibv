# Proposed TRON contribution to rotki

Discussion target: [rotki/rotki#10465](https://github.com/rotki/rotki/issues/10465).

Rotki BV implements TRON wallet support on the upstream v1.44.1 base: canonical addresses and TRC20 contract identity, a TronScan client with user-managed API keys, wallet balances, transaction synchronization, fees, shared history events and frontend account/history flows. The implementation and regression corpus are available in this public fork.

The initial contribution would cover wallet TRX/TRC20 and ordinary transfers. It deliberately excludes staking, frozen/resource positions, TRC10, NFTs and protocol-specific DeFi decoding. It depends on TronScan availability and a user-supplied API key; queried addresses and transaction hashes are sent to that provider.

Those additional capabilities are tracked separately in [the extended TRON plan](tron-extended-support.md) and [epic #35](https://github.com/brylovit78/rotkibv/issues/35). They are not implemented by this proposal or included in its current support claim.

The implementation reuses rotki's managers, exact amount arithmetic and shared history events. Tests cover provider errors, pagination boundaries, confirmation and failure states, transfer identity, interrupted synchronization, account removal, user-edited events and API/UI integration. The recorded provider corpus distinguishes observed responses from synthetic edge cases.

Before requesting an upstream merge, the contribution needs a clean port to current `develop`, standard upstream migrations and agreed enum/asset allocations. Fork-only startup schema extensions, branding, GHCR workflows, version-tag handling and deployment tooling would be excluded. Existing upstream equivalents of test stabilization and secret-log redaction would be retained rather than duplicated.

Suggested review sequence, subject to maintainer preference:

1. Address/asset identity, migrations and the TronScan client.
2. Accounts and wallet balances, with API-key/account UI.
3. Transaction storage/synchronization, decoding and history UI.

Each stage should build and pass its relevant tests independently. The final port needs full upstream CI, fresh/upgrade database tests and browser E2E on its exact revision. The stable fork release is available for evaluation; this document does not claim that an upstream-compatible branch has already been prepared or accepted.

Before porting, agree the provider choice, supported scope, asset-repository changes and preferred PR split with the maintainer. Follow the upstream issue/tests/CLA process. AML and crypto-risk screening are separate, unimplemented design work and are not part of this proposal.

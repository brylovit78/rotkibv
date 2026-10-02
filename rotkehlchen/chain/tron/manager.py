"""TRON wallet balances, see docs/designs/tron-integration.md sections 3.2, 3.3 and 5"""
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Final

from rotkehlchen.accounting.structures.balance import Balance, BalanceSheet
from rotkehlchen.assets.asset import Asset, CryptoAsset
from rotkehlchen.assets.utils import token_normalized_value_decimals
from rotkehlchen.chain.manager import ChainManagerWithTransactions
from rotkehlchen.chain.tron.constants import TRX_DECIMALS
from rotkehlchen.chain.tron.decoding import TronTransactionDecoder
from rotkehlchen.chain.tron.transactions import TronTransactions
from rotkehlchen.chain.tron.utils import (
    deserialize_raw_amount,
    deserialize_tron_address,
    get_or_create_tron_token,
)
from rotkehlchen.constants import DEFAULT_BALANCE_LABEL
from rotkehlchen.errors.misc import MissingAPIKey, RemoteError
from rotkehlchen.errors.serialization import DeserializationError
from rotkehlchen.externalapis.tronscan import TRONSCAN_HOLDINGS_LIMIT
from rotkehlchen.globaldb.handler import GlobalDBHandler
from rotkehlchen.inquirer import Inquirer
from rotkehlchen.types import SupportedBlockchain, Timestamp, TronAddress, TronTxHash

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rotkehlchen.db.dbhandler import DBHandler
    from rotkehlchen.externalapis.tronscan import Tronscan
    from rotkehlchen.fval import FVal

NON_WALLET_TOKEN_TYPES: Final = ('trc10', 'trc721', 'trc1155')  # TRX is the "_" trc10 row
# Holdings pagination is verified only within the first 10000 rows. A complete list must end
# with a short page below that, otherwise the snapshot can not be proven complete.
TRON_HOLDINGS_MAX_START: Final = 10000


class TronManager(ChainManagerWithTransactions[TronAddress]):

    def __init__(self, tronscan: Tronscan, database: DBHandler) -> None:
        self.tronscan = tronscan
        self.database = database
        self.transactions = TronTransactions(tronscan=tronscan, database=database)
        self.decoder = TronTransactionDecoder(database=database, tronscan=tronscan)

    def _query_holdings(self, address: TronAddress) -> list[dict[str, Any]]:
        """All token holdings of an account. A short page ends the list, never the total.

        May raise MissingAPIKey, RemoteError, DeserializationError.
        """
        holdings: list[dict[str, Any]] = []
        for start in range(0, TRON_HOLDINGS_MAX_START, TRONSCAN_HOLDINGS_LIMIT):
            holdings.extend(page := self.tronscan.query_holdings_page(address, start=start))
            if len(page) < TRONSCAN_HOLDINGS_LIMIT:
                return holdings

        raise RemoteError(
            f'TronScan lists more than {TRON_HOLDINGS_MAX_START} token holdings for {address}, '
            f'so the balance snapshot can not be proven complete',
        )

    def _token(self, contract: TronAddress, decimals: Any) -> CryptoAsset:
        """The asset of a TRC20 contract that a holdings row reports with the given decimals.
        An unknown contract is created from its validated TronScan metadata.

        Disagreeing decimals fail before anything is stored, so corrected provider data is
        accepted on a later query.

        May raise MissingAPIKey, RemoteError, DeserializationError.
        """
        with GlobalDBHandler().conn.read_ctx() as cursor:
            row = cursor.execute(
                'SELECT identifier, decimals FROM tron_tokens WHERE address=?', (contract,),
            ).fetchone()
        if row is not None:
            if row[1] != decimals:
                raise DeserializationError(f'TRC20 {contract} has {decimals!r} decimals in the holdings instead of {row[1]}')  # noqa: E501
            return CryptoAsset(row[0])

        if (metadata := self.tronscan.query_token_metadata(contract)) is None:
            raise RemoteError(f'TronScan returned no metadata for TRC20 contract {contract}')
        if type(metadata_decimals := metadata['decimals']) is not int or metadata_decimals != decimals:  # noqa: E501
            raise DeserializationError(f'TRC20 {contract} has {decimals!r} decimals in the holdings but {metadata_decimals!r} in its metadata')  # noqa: E501
        if not isinstance(name := metadata.get('name'), str) or not isinstance(symbol := metadata.get('symbol'), str):  # noqa: E501
            raise DeserializationError(f'Invalid name or symbol for TRC20 contract {contract}')

        get_or_create_tron_token(self.database, contract, name=name, symbol=symbol, decimals=decimals)  # noqa: E501
        return self._token(contract, decimals)  # a concurrent query may have stored it first

    def _account_amounts(self, address: TronAddress) -> dict[Asset, FVal]:
        """Exact TRX and TRC20 amounts of an account. Every row must parse, so a partial list is
        never returned. TRX comes from accountv2. The holdings row of TRX (token id "_") and
        TRC10, TRC721 and TRC1155 rows are not wallet TRC20 balances.

        May raise MissingAPIKey, RemoteError, DeserializationError, KeyError.
        """
        amounts: dict[Asset, FVal] = {}
        if (sun := deserialize_raw_amount(self.tronscan.query_account(address)['balanceStr'])) != 0:  # noqa: E501
            amounts[Asset(SupportedBlockchain.TRON.get_native_token_id())] = token_normalized_value_decimals(sun, TRX_DECIMALS)  # noqa: E501

        for row in self._query_holdings(address):
            if row['tokenId'] == '_' or row['tokenType'] in NON_WALLET_TOKEN_TYPES:
                continue
            if row['tokenType'] != 'trc20':  # an unknown row could hide a wallet balance
                raise DeserializationError(f'Unknown TronScan token type in {row} for {address}')
            if type(decimals := row['tokenDecimal']) is not int:  # bool is also an int
                raise DeserializationError(f'Invalid TRC20 decimals in {row} for {address}')
            if (raw := deserialize_raw_amount(row['balance'])) != 0:
                token = self._token(deserialize_tron_address(row['tokenId']), decimals)
                amounts[token] = token_normalized_value_decimals(raw, decimals)

        return amounts

    def query_balances(
            self,
            addresses: Sequence[TronAddress],
    ) -> dict[TronAddress, BalanceSheet]:
        """Query the wallet TRX and TRC20 balances of the given accounts.

        All accounts complete or the query fails, so the aggregator keeps the previous balances
        instead of a partial or zero snapshot.

        May raise RemoteError if TronScan fails, the key is missing or any provider data is
        malformed or incomplete.
        """
        try:
            amounts = {address: self._account_amounts(address) for address in addresses}
        except MissingAPIKey as e:
            raise RemoteError('Querying TRON balances needs a TronScan API key') from e
        except (DeserializationError, KeyError) as e:
            raise RemoteError(f'Malformed TronScan balance data: {e!s}') from e

        with self.database.conn.read_ctx() as cursor:  # read after discovery, so new spam is in it
            ignored = self.database.get_ignored_asset_ids(cursor)
        amounts = {address: {asset: amount for asset, amount in x.items() if asset.identifier not in ignored} for address, x in amounts.items()}  # noqa: E501
        prices = Inquirer.find_main_currency_prices(list({asset for x in amounts.values() for asset in x}))  # noqa: E501
        balances: defaultdict[TronAddress, BalanceSheet] = defaultdict(BalanceSheet)
        for address, account_amounts in amounts.items():  # an empty account is a zero snapshot
            for asset, amount in account_amounts.items():
                balances[address].assets[asset][DEFAULT_BALANCE_LABEL] = Balance(
                    amount=amount,
                    value=amount * prices[asset],
                )

        return dict(balances)

    def query_transactions(
            self,
            addresses: list[TronAddress],
            from_timestamp: Timestamp,
            to_timestamp: Timestamp,
            refetch: bool = False,
    ) -> None:
        """Sync the history feeds of the given accounts, see TronTransactions, then decode
        what waits for decoding.

        May raise RemoteError.
        """
        try:
            self.transactions.query_transactions(addresses, from_timestamp, to_timestamp, refetch)
        except MissingAPIKey as e:
            raise RemoteError('Querying TRON history needs a TronScan API key') from e

        self.decoder.decode_transactions()

    def refetch_transactions(
            self,
            address: TronAddress,
            from_timestamp: Timestamp,
            to_timestamp: Timestamp,
    ) -> list[TronTxHash]:
        """Read the history feeds of the account over the range again and decode. Returns
        the hashes of the transactions that were not stored before.

        May raise RemoteError.
        """
        with self.database.conn.read_ctx() as cursor:
            stored = {x[0] for x in cursor.execute('SELECT tx_hash FROM tron_transactions')}

        self.query_transactions([address], from_timestamp, to_timestamp, refetch=True)
        with self.database.conn.read_ctx() as cursor:
            return [TronTxHash(x[0].hex()) for x in cursor.execute('SELECT tx_hash FROM tron_transactions') if x[0] not in stored]  # noqa: E501

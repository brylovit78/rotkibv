"""TRON wallet balances, see docs/designs/tron-integration.md sections 3.2, 3.3 and 5"""
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Final

from rotkehlchen.accounting.structures.balance import Balance, BalanceSheet
from rotkehlchen.assets.asset import Asset, CryptoAsset
from rotkehlchen.assets.utils import token_normalized_value_decimals
from rotkehlchen.chain.manager import ChainManager
from rotkehlchen.chain.tron.utils import deserialize_tron_address, get_or_create_tron_token
from rotkehlchen.constants import DEFAULT_BALANCE_LABEL
from rotkehlchen.errors.misc import MissingAPIKey, RemoteError
from rotkehlchen.errors.serialization import DeserializationError
from rotkehlchen.externalapis.tronscan import TRONSCAN_HOLDINGS_LIMIT
from rotkehlchen.globaldb.handler import GlobalDBHandler
from rotkehlchen.inquirer import Inquirer
from rotkehlchen.logging import RotkehlchenLogsAdapter
from rotkehlchen.types import SupportedBlockchain, TronAddress

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rotkehlchen.db.dbhandler import DBHandler
    from rotkehlchen.externalapis.tronscan import Tronscan
    from rotkehlchen.fval import FVal

logger = logging.getLogger(__name__)
log = RotkehlchenLogsAdapter(logger)

TRX_DECIMALS: Final = 6  # balances are in sun
# Holdings pagination is verified only within the first 10000 rows. A complete list must end
# with a short page below that, otherwise the snapshot can not be proven complete.
TRON_HOLDINGS_MAX_START: Final = 10000


def _raw_amount(value: Any) -> int:
    """May raise DeserializationError if the value is not a decimal string of raw units"""
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise DeserializationError(f'Invalid raw amount {value!r}')
    return int(value)


class TronManager(ChainManager[TronAddress]):

    def __init__(self, tronscan: Tronscan, database: DBHandler) -> None:
        self.tronscan = tronscan
        self.database = database

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

    def _token(self, contract: TronAddress) -> tuple[CryptoAsset, int]:
        """The asset and decimals of a TRC20 contract. An unknown contract is created from
        its validated TronScan metadata.

        May raise MissingAPIKey, RemoteError, DeserializationError.
        """
        with GlobalDBHandler().conn.read_ctx() as cursor:
            if (row := cursor.execute(
                'SELECT identifier, decimals FROM tron_tokens WHERE address=?', (contract,),
            ).fetchone()) is not None:
                return CryptoAsset(row[0]), row[1]

        if (metadata := self.tronscan.query_token_metadata(contract)) is None:
            raise RemoteError(f'TronScan returned no metadata for TRC20 contract {contract}')
        if not isinstance(name := metadata.get('name'), str) or not isinstance(symbol := metadata.get('symbol'), str):  # noqa: E501
            raise DeserializationError(f'Invalid name or symbol for TRC20 contract {contract}')

        decimals = metadata['decimals']  # validated by get_or_create_tron_token
        return get_or_create_tron_token(self.database, contract, name=name, symbol=symbol, decimals=decimals), decimals  # noqa: E501

    def _account_amounts(self, address: TronAddress) -> dict[Asset, FVal]:
        """Exact TRX and TRC20 amounts of an account. Every row must parse, so a partial list is
        never returned. TRX comes from accountv2. The holdings row of TRX (token id "_") and
        TRC10, TRC721 and TRC1155 rows are not wallet TRC20 balances.

        May raise MissingAPIKey, RemoteError, DeserializationError, KeyError.
        """
        amounts: dict[Asset, FVal] = {}
        if (sun := _raw_amount(self.tronscan.query_account(address)['balanceStr'])) != 0:
            amounts[Asset(SupportedBlockchain.TRON.get_native_token_id())] = token_normalized_value_decimals(sun, TRX_DECIMALS)  # noqa: E501

        for row in self._query_holdings(address):
            if row['tokenId'] == '_' or row['tokenType'] != 'trc20' or (raw := _raw_amount(row['balance'])) == 0:  # noqa: E501
                continue

            token, decimals = self._token(deserialize_tron_address(row['tokenId']))
            if row['tokenDecimal'] != decimals:  # the stored decimals of a contract are final
                raise DeserializationError(f'TRC20 {row["tokenId"]} of {address} has {row["tokenDecimal"]} decimals instead of {decimals}')  # noqa: E501
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

        prices = Inquirer.find_main_currency_prices(list({asset for x in amounts.values() for asset in x}))  # noqa: E501
        balances: defaultdict[TronAddress, BalanceSheet] = defaultdict(BalanceSheet)
        for address, account_amounts in amounts.items():  # an empty account is a zero snapshot
            for asset, amount in account_amounts.items():
                balances[address].assets[asset][DEFAULT_BALANCE_LABEL] = Balance(
                    amount=amount,
                    value=amount * prices[asset],
                )

        return dict(balances)

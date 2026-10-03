"""Fail the rehearsal on lost data, a pending assets update or a fork asset out of place."""
# Stdlib runner keeps delivery checks independent of application dependencies.
# ruff: noqa: PT009, PT027
import contextlib
import io
import unittest

from docker_rehearse import (
    FORK_ASSETS,
    TRON_ACCOUNT,
    check,
    fork_assets,
    kept,
    stored,
    update_offered,
)

STATE = {'tags': ['Contract', 'rehearsal'], 'kraken_events': 1, 'db_version': 53}
TRON = {'tron_accounts': [TRON_ACCOUNT], 'tron_events': 1}


class DockerRehearseTests(unittest.TestCase):
    def test_test_data_must_be_in_the_profile(self):
        self.assertTrue(stored(STATE, None))
        self.assertTrue(stored(STATE | TRON, 1))
        self.assertFalse(stored(dict(STATE, tags=['Contract']), None))
        self.assertFalse(stored(dict(STATE, kraken_events=0), None))
        self.assertFalse(stored(STATE, 1))
        self.assertFalse(stored(STATE | TRON, 2))
        self.assertFalse(stored(STATE | dict(TRON, tron_accounts=[]), 1))

    def test_upgrade_keeps_old_values_and_may_raise_the_db_version(self):
        self.assertTrue(kept(STATE, STATE | TRON | {'db_version': 54}))
        self.assertFalse(kept(STATE, dict(STATE, kraken_events=0)))
        self.assertFalse(kept(STATE | TRON, STATE))
        self.assertFalse(kept(STATE | TRON, STATE | dict(TRON, tron_events=2)))

    def test_fork_assets_are_read_with_their_collections(self):
        trx, usdt = FORK_ASSETS
        assets = {
            trx: {'symbol': 'TRX', 'collection_id': '23'},
            usdt: {'symbol': 'USDT', 'collection_id': '9'},
        }
        collections = {'23': {'symbol': 'TRX'}, '9': {'symbol': 'USDT'}}
        mappings = {'assets': assets, 'asset_collections': collections}
        self.assertEqual(fork_assets(mappings), FORK_ASSETS)
        # an assets update that drops the asset, its collection mapping or moves it
        self.assertEqual(fork_assets({'assets': {trx: assets[trx]}})[usdt], None)
        self.assertEqual(
            fork_assets(dict(mappings, assets=assets | {usdt: {'symbol': 'USDT'}}))[usdt],
            ('USDT', None),
        )
        self.assertEqual(
            fork_assets(dict(mappings, asset_collections={'9': {'symbol': 'TRX'}})),
            {trx: ('TRX', None), usdt: ('USDT', 'TRX')},
        )

    def test_update_is_offered_as_in_the_frontend(self):
        self.assertTrue(update_offered({'local': 41, 'remote': 42, 'new_changes': 9}))
        self.assertFalse(update_offered({'local': 42, 'remote': 42, 'new_changes': 0}))
        # remote updates that the local schema version cannot take are not offered
        self.assertFalse(update_offered({'local': 42, 'remote': 43, 'new_changes': 0}))
        self.assertFalse(update_offered({'local': 42, 'remote': 42, 'new_changes': 9}))

    def test_failed_check_stops_with_a_nonzero_exit(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            check('new', 'user data kept', True)
            with self.assertRaises(SystemExit) as failure:
                check('new', 'user data kept', False)
        self.assertEqual(failure.exception.code, 'FAIL: new: user data kept')
        self.assertEqual(
            output.getvalue(), 'new: user data kept True\nnew: user data kept False\n',
        )


if __name__ == '__main__':
    unittest.main()

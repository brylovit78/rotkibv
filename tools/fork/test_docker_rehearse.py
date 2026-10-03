"""Fail the rehearsal on lost data, a pending assets update or a fork asset out of place."""
# Stdlib runner keeps delivery checks independent of application dependencies.
# ruff: noqa: PT009, PT027
import contextlib
import io
import unittest

from docker_rehearse import (
    EVENT,
    FORK_ASSETS,
    TAG,
    TRON_ACCOUNT,
    TRON_EVENTS,
    chain_ids,
    check,
    count,
    envelope,
    fork_assets,
    kept,
    snapshot,
    stored,
    update_offered,
)

# answers of the API, in the shape the images give them
TAGS = {'Contract': dict(TAG, name='Contract', description='System tag'), 'rehearsal': TAG}
ACCOUNTS = [{'address': TRON_ACCOUNT, 'label': None, 'tags': None}]


def history(*events):
    entries = [{'entry': x | {'identifier': 1, 'extra_data': None}, 'states': []} for x in events]
    return {'entries': entries, 'entries_found': len(events)}


STATE = {'tag': TAG, 'kraken_events': [EVENT], 'db_version': 53}
TRON = {'tron_accounts': [TRON_ACCOUNT], 'tron_events': TRON_EVENTS[:1]}


class DockerRehearseTests(unittest.TestCase):
    def test_snapshot_holds_the_test_data_as_the_image_returns_it(self):
        self.assertEqual(snapshot(TAGS, history(EVENT), 53), STATE)
        newest_first = history(*reversed(TRON_EVENTS))
        self.assertEqual(
            snapshot(TAGS, history(EVENT), 53, tron=(ACCOUNTS, newest_first)),
            STATE | {'tron_accounts': [TRON_ACCOUNT], 'tron_events': TRON_EVENTS},
        )
        # a system tag that a migration adds is not the rehearsal's data
        self.assertEqual(snapshot(TAGS | {'System': TAGS['Contract']}, history(EVENT), 53), STATE)
        # an event that the image cannot read is counted but not listed
        unreadable = {'entries': [], 'entries_found': 1}
        self.assertEqual(snapshot(TAGS, unreadable, 53)['kraken_events'], [])
        changed = dict(EVENT, amount='15')
        self.assertEqual(snapshot(TAGS, history(changed), 53)['kraken_events'], [changed])
        lost = {'rehearsal': dict(TAG, description='lost')}
        self.assertEqual(snapshot(lost, history(EVENT), 53)['tag'], lost['rehearsal'])
        # failed requests give no data, not an error
        self.assertEqual(
            snapshot(None, None, None, tron=(None, None)),
            {'tag': dict.fromkeys(TAG), 'kraken_events': [], 'db_version': None} |
            {'tron_accounts': [], 'tron_events': []},
        )

    def test_only_a_listed_chain_set_decides_on_tron(self):
        chains = [{'id': 'eth', 'name': 'ethereum'}, {'id': 'tron', 'name': 'tron'}]
        self.assertEqual(chain_ids(200, chains), ['eth', 'tron'])
        self.assertEqual(chain_ids(200, chains[:1]), ['eth'])
        # a failed or malformed answer is not an image without TRON
        self.assertIsNone(chain_ids(503, None))
        self.assertIsNone(chain_ids(401, chains))
        self.assertIsNone(chain_ids(200, []))
        for broken in ({'name': 'TRON'}, {'id': None}, {'id': ''}, {'id': 100}, 'tron'):
            self.assertIsNone(chain_ids(200, [chains[0], broken]))
        self.assertIsNone(chain_ids(200, {'id': 'eth'}))

    def test_answer_must_be_a_json_object(self):
        self.assertEqual(envelope(b'{"result": null, "message": "no"}')['message'], 'no')
        self.assertEqual(envelope(b''), {})
        for raw in (b'null', b'[]', b'"text"', b'<html>502</html>'):
            with self.assertRaises(ValueError):
                envelope(raw)
        # the informational step reads a count from whatever result the old image gives
        self.assertEqual(count({'entries': [], 'entries_found': 3}), 3)
        for result in (None, [3], 'text', True, {}):
            self.assertIsNone(count(result))

    def test_test_data_must_come_back_as_stored(self):
        self.assertTrue(stored(STATE, None))
        self.assertTrue(stored(STATE | TRON, TRON_EVENTS[:1]))
        self.assertFalse(stored(dict(STATE, tag=dict(TAG, description='lost')), None))
        self.assertFalse(stored(dict(STATE, kraken_events=[]), None))
        self.assertFalse(stored(dict(STATE, kraken_events=[dict(EVENT, amount='15')]), None))
        self.assertFalse(stored(STATE, TRON_EVENTS[:1]))
        self.assertFalse(stored(STATE | TRON, TRON_EVENTS))
        self.assertFalse(stored(STATE | dict(TRON, tron_accounts=[]), TRON_EVENTS[:1]))
        self.assertFalse(stored(dict(STATE, db_version=None), None))

    def test_upgrade_keeps_old_values_and_may_raise_the_db_version(self):
        self.assertTrue(kept(STATE, STATE))
        self.assertTrue(kept(STATE, STATE | TRON | {'db_version': 54}))
        # the settings request failed, or a version that only a broken upgrade can report
        self.assertFalse(kept(STATE, dict(STATE, db_version=None)))
        self.assertFalse(kept(STATE, dict(STATE, db_version=52)))
        self.assertFalse(kept(STATE, dict(STATE, tag=dict(TAG, description='lost'))))
        self.assertFalse(kept(STATE, dict(STATE, kraken_events=[])))
        self.assertFalse(kept(STATE | TRON, STATE))
        self.assertFalse(kept(STATE | TRON, STATE | dict(TRON, tron_events=TRON_EVENTS)))

    def test_fork_assets_are_read_with_their_collections(self):
        (trx, (_, trx_main)), (usdt, (_, usdt_main)) = FORK_ASSETS.items()
        assets = {
            trx: {'symbol': 'TRX', 'collection_id': '332'},
            usdt: {'symbol': 'USDT', 'collection_id': '37'},
        }
        collections = {
            '332': {'symbol': 'TRX', 'main_asset': trx_main},
            '37': {'symbol': 'USDT', 'main_asset': usdt_main},
        }
        mappings = {'assets': assets, 'asset_collections': collections}
        self.assertEqual(fork_assets(mappings), FORK_ASSETS)
        # an assets update that drops the asset, renames it, or takes it out of its collection
        self.assertEqual(fork_assets({'assets': {trx: assets[trx]}})[usdt], None)
        renamed = assets | {usdt: {'symbol': 'USDT.e', 'collection_id': '37'}}
        self.assertEqual(fork_assets(dict(mappings, assets=renamed))[usdt], ('USDT.e', usdt_main))
        loose = assets | {usdt: {'symbol': 'USDT'}}
        self.assertEqual(fork_assets(dict(mappings, assets=loose))[usdt], ('USDT', None))
        # another collection with the same symbol is not the fork's collection
        other = collections | {'37': {'symbol': 'USDT', 'main_asset': 'eip155:10/erc20:0x94b0'}}
        self.assertNotEqual(fork_assets(dict(mappings, asset_collections=other)), FORK_ASSETS)

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

"""Docker backup, upgrade, restore and rollback rehearsal of a fork image on test volumes.

Uses its own Compose project, volumes and port, so no other deployment is touched. No TronScan
key and no real data. The profile password is generated here and kept only in memory.

Exits with a non-zero code when a check that must hold does not: the test data after the upgrade
and after the rollback, the in-app assets update and the fork assets. What the old image does on
the migrated volumes without the backup is only reported.

usage: docker_rehearse.py <compose file> <old image ref> <new image ref>
"""
# Docker is a trusted local CLI, invoked without a shell. URLs are loopback HTTP only.
# ruff: noqa: S404, S603, S607, S310
import contextlib
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from http.client import HTTPException
from pathlib import Path

PROJECT, PORT = 'rotkibv-rehearsal', '18080'
API = f'http://127.0.0.1:{PORT}/api/1'
USER, PASSWORD = 'rbtest', secrets.token_urlsafe(18)
VOLUME_DIRS = ('data', 'logs', 'config')
# The test data, as the API takes it and gives it back
TAG = {
    'name': 'rehearsal', 'description': 'kept',
    'background_color': 'ffffff', 'foreground_color': '000000',
}
EVENT = {
    'entry_type': 'history event', 'timestamp': 1700000000000, 'sequence_index': 0,
    'group_identifier': 'rehearsal-1', 'location': 'kraken', 'event_type': 'receive',
    'event_subtype': 'none', 'asset': 'ETH', 'amount': '1.5', 'user_notes': 'rehearsal event',
}
TRON_ACCOUNT = 'TQhqRHgEonKEYqudomS8243o3bejg8dt1d'  # synthetic: 41 and twenty a1 bytes
# the first comes with the account, the second is added by the new image to an old TRON profile
TRON_EVENTS = [{
    'entry_type': 'tron event', 'tx_ref': tx_ref, 'timestamp': 1700000100000,
    'sequence_index': 0, 'location_label': TRON_ACCOUNT, 'event_type': 'receive',
    'event_subtype': 'none', 'asset': 'TRX', 'amount': '2',
    'user_notes': 'rehearsal TRON event',
} for tx_ref in ('ab' * 32, 'cd' * 32)]
# seeded by the fork: the symbol of the asset and the main asset of its collection
FORK_ASSETS = {
    'TRX': ('TRX', 'eip155:1/erc20:0x50327c6c5a14DCaDE707ABad2E27eB517df87AB5'),
    'tron/trc20:41a614f803b6fd780986a42c78ec9c7f77e6ded13c': (
        'USDT', 'eip155:1/erc20:0xdAC17F958D2ee523a2206206994597C13D831ec7',
    ),
}


def seeded(found, fields):
    """What the API returned for the fields that the rehearsal set"""
    return {key: found.get(key) for key in fields}


def listed(history, fields):
    """The events of a history answer by their seeded fields. An event that the image cannot
    read is counted in `entries_found` but not listed, so the count proves nothing"""
    entries = (history or {}).get('entries') or []
    return sorted((seeded(x['entry'], fields) for x in entries), key=str)


def snapshot(tags, history, db_version, tron=None):
    """The test data in the answers of an image. `tron` is its TRON accounts and TRON history,
    None for an image without TRON"""
    state = {
        'tag': seeded((tags or {}).get(TAG['name']) or {}, TAG),
        'kraken_events': listed(history, EVENT),
        'db_version': db_version,
    }
    if tron is not None:
        accounts, tron_history = tron
        state |= {
            'tron_accounts': [x['address'] for x in accounts or []],
            'tron_events': listed(tron_history, TRON_EVENTS[0]),
        }
    return state


def chain_ids(status, chains):
    """The ids of the chains that an image lists as supported. None for any other answer, so
    that a failed request never reads as an image without TRON"""
    if status != 200 or not isinstance(chains, list) or not chains:
        return None
    ids = [x.get('id') if isinstance(x, dict) else None for x in chains]
    return ids if all(isinstance(x, str) and x for x in ids) else None


def stored(state, tron_events):
    """The image returns the test data as it was stored; `tron_events` is None without TRON"""
    return state['tag'] == TAG and state['kraken_events'] == [EVENT] and (
        tron_events is None or
        (state.get('tron_accounts'), state.get('tron_events')) == ([TRON_ACCOUNT], tron_events)
    )


def kept(before, after):
    """Every value the old image reported is unchanged; an upgrade may raise the DB version"""
    return all(after.get(key) == value for key, value in before.items() if key != 'db_version')


def fork_assets(mappings):
    """The symbol of each fork asset and the main asset of its collection, which identifies the
    collection (symbols repeat); None for a missing asset"""
    assets = mappings.get('assets') or {}
    collections = mappings.get('asset_collections') or {}
    return {
        x: (
            assets[x].get('symbol'),
            (collections.get(str(assets[x].get('collection_id'))) or {}).get('main_asset'),
        ) if x in assets else None
        for x in FORK_ASSETS
    }


def update_offered(versions):
    """Whether the UI offers an assets update after a login (frontend `use-assets.ts`)"""
    return versions['local'] < versions['remote'] and versions['new_changes'] > 0


def check(label, what, ok):
    """Report a check that must hold, and stop the rehearsal when it does not"""
    print(f'{label}: {what} {ok}')
    if not ok:
        raise SystemExit(f'FAIL: {label}: {what}')


def docker(*args, image=None):
    # These win over the deployment's `.env`: loopback, the rehearsal port, and no session key,
    # as `api` keeps no session cookie
    env = os.environ | {
        'ROTKIBV_BIND': '127.0.0.1',
        'ROTKIBV_PORT': PORT,
        'ROTKI_SESSION_KEY': '',
        'DOCKER_DEFAULT_PLATFORM': 'linux/amd64',
    } | ({'ROTKIBV_IMAGE': image} if image else {})
    return subprocess.check_output(['docker', *args], text=True, env=env).strip()


def envelope(raw):
    """The JSON object that every answer of the API is; anything else is a ValueError"""
    answer = json.loads(raw or b'{}')
    if not isinstance(answer, dict):
        raise ValueError(f'the API answered {answer!r}, not a JSON object')
    return answer


def api(method, path, body=None):
    request = urllib.request.Request(
        f'{API}/{path}',
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={} if body is None else {'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, envelope(response.read())
    except urllib.error.HTTPError as e:
        return e.code, envelope(e.read())


def result_of(method, path, body=None):
    return api(method, path, body)[1].get('result')


def said(body):
    return str(body.get('message', ''))[:160]


def call(label, what, method, path, body):
    status, answer = api(method, path, body)
    print(f'{label}: {what} HTTP {status} {said(answer)!r}')
    return status


def login(label):
    body = {'password': PASSWORD, 'sync_approval': 'no'}
    return call(label, 'login', 'POST', f'users/{USER}', body) == 200


def logout():
    api('PATCH', f'users/{USER}', {'action': 'logout'})


def supports_tron(label):
    status, body = api('GET', 'blockchains/supported')
    chains = chain_ids(status, body.get('result'))
    check(label, f'supported chains listed (HTTP {status})', chains is not None)
    print(f'{label}: TRON', 'supported' if 'tron' in chains else 'not supported')
    return 'tron' in chains


def count(history):
    """How many events a history answer found; None for an answer without that number"""
    return history.get('entries_found') if isinstance(history, dict) else None


def entries_found(**filters):
    return count(result_of('POST', 'history/events', filters))


def all_events(label):
    status, body = api('POST', 'history/events', {})
    found = count(body.get('result'))
    print(f'{label}: all history events HTTP {status} found {found} {said(body)!r}')


def add_tron_event(label, event):
    call(label, 'add TRON event', 'PUT', 'history/events', event)
    print(f'{label}: TRON events', entries_found(location='tron'))


def add_tron_data(label):
    for what, address in (
            ('add TRON account', TRON_ACCOUNT),
            ('add it again', TRON_ACCOUNT),
            ('add an invalid address', TRON_ACCOUNT[:-1] + 'e'),
    ):
        call(label, what, 'PUT', 'blockchains/tron/accounts', {'accounts': [{'address': address}]})
    add_tron_event(label, TRON_EVENTS[0])


def check_fork_assets(label):
    mappings = result_of('POST', 'assets/mappings', {'identifiers': list(FORK_ASSETS)})
    found = fork_assets(mappings or {})
    check(label, f'fork assets {found} in their collections', found == FORK_ASSETS)


def assets_update(label):
    """The in-app assets update, as the UI offers it after a login"""
    status, body = api('GET', 'assets/updates')
    versions = body.get('result') or {}
    check(label, f'assets update check HTTP {status} {versions or said(body)!r}', bool(versions))
    check_fork_assets(label)
    if not update_offered(versions):
        print(f'{label}: no assets update is offered')
        return
    status, body = api('POST', 'assets/updates', {})
    versions = result_of('GET', 'assets/updates') or {}
    check(
        label,
        f'assets update HTTP {status} {said(body)!r} applied, now {versions}',
        status == 200 and bool(versions) and not update_offered(versions),
    )
    check_fork_assets(label)


def user_data(label, tron):
    """The test data as the image returns it; `tron` tells whether the image supports TRON"""
    state = snapshot(
        tags=result_of('GET', 'tags'),
        history=result_of('POST', 'history/events', {'location': 'kraken'}),
        db_version=(result_of('GET', 'settings') or {}).get('version'),
        tron=(
            result_of('GET', 'blockchains/tron/accounts'),
            result_of('POST', 'history/events', {'location': 'tron'}),
        ) if tron else None,
    )
    version = (result_of('GET', 'info') or {}).get('version') or {}
    print(f'{label}: {state} app version {version.get("our_version")}')
    return state


def image_facts(label, container_id):
    image = json.loads(docker('inspect', container_id))[0]['Image']
    facts = json.loads(docker('inspect', image))[0]
    labels = facts['Config']['Labels']
    print(
        f'{label}: image {facts["RepoDigests"]} '
        f'revision {labels.get("org.opencontainers.image.revision")} '
        f'version {labels.get("org.opencontainers.image.version")}',
    )


def main():
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    compose_file, old, new = sys.argv[1:]
    sys.stdout.reconfigure(line_buffering=True)  # stay in step with Docker's stderr

    def compose(image, *args):
        return docker('compose', '-p', PROJECT, '-f', compose_file, *args, image=image)

    def up(image):
        compose(image, 'up', '-d', '--wait', '--wait-timeout', '900')
        return compose(image, 'ps', '-aq', 'rotki')

    if compose(old, 'ps', '-aq'):
        down = shlex.join(['docker', 'compose', '-p', PROJECT, '-f', compose_file, 'down', '-v'])
        raise SystemExit(
            f'The {PROJECT} project already has containers. If no rehearsal is running, '
            f'remove them: ROTKIBV_IMAGE=none {down}',
        )
    try:
        with tempfile.TemporaryDirectory() as backup:
            # 1. the previous image creates a profile with some user data on fresh test volumes
            image_facts('old', container_id := up(old))
            call('old', 'create profile', 'PUT', 'users', {
                'name': USER, 'password': PASSWORD,
                'initial_settings': {'submit_usage_analytics': False},
            })
            call('old', 'add tag', 'PUT', 'tags', TAG)
            call('old', 'add event', 'PUT', 'history/events', EVENT)
            if old_has_tron := supports_tron('old'):
                add_tron_data('old')
            before = user_data('old', old_has_tron)
            expected = TRON_EVENTS[:1] if old_has_tron else None
            check('old', 'test data stored', stored(before, expected))
            logout()

            # 2. stop and back up every volume
            compose(old, 'stop')
            for directory in VOLUME_DIRS:
                docker('cp', f'{container_id}:/{directory}', f'{backup}/{directory}')
            print('backup:', {
                d: sum(1 for x in Path(backup, d).rglob('*') if x.is_file()) for d in VOLUME_DIRS
            }, 'files per volume')

            # 3. the new image on the same volumes
            image_facts('new', up(new))
            check('new', 'profile opens', login('new'))
            check('new', 'user data kept', kept(before, user_data('new', tron=True)))
            supports_tron('new')
            if old_has_tron:
                add_tron_event('new', TRON_EVENTS[1])
            else:
                add_tron_data('new')
            with_tron = user_data('new', tron=True)
            expected = TRON_EVENTS if old_has_tron else TRON_EVENTS[:1]
            check('new', 'TRON data stored', stored(with_tron, expected))
            assets_update('new')
            after_update = user_data('new', tron=True)
            check('new', 'user data kept after the assets update', after_update == with_tron)
            logout()

            # 4. a downgrade without the backup, on the migrated volumes (never do this on real
            # data). Informational: whatever the old image does here, the rehearsal goes on
            try:
                up(old)
                if login('downgrade without restore'):
                    all_events('downgrade without restore')
                    if old_has_tron:
                        found = entries_found(location='tron')
                        print('downgrade without restore: TRON events', found)
                    logout()
            except (subprocess.CalledProcessError, OSError, ValueError, HTTPException) as e:
                print(f'downgrade without restore: failed, {e}')
                with contextlib.suppress(subprocess.CalledProcessError, OSError, ValueError):
                    print(compose(old, 'logs', '--tail', '20'))

            # 5. rollback: the previous image with its matching backup on new volumes
            compose(old, 'down', '-v')
            compose(old, 'create')
            container_id = compose(old, 'ps', '-aq', 'rotki')
            for directory in VOLUME_DIRS:
                docker('cp', f'{backup}/{directory}/.', f'{container_id}:/{directory}')
            image_facts('rollback', up(old))
            check('rollback', 'profile opens', login('rollback'))
            check('rollback', 'user data kept', user_data('rollback', old_has_tron) == before)
            all_events('rollback')
            logout()
    finally:
        # 6. remove the rehearsal containers, volumes and backup
        compose(old, 'down', '-v')
        print('cleanup: done')
    print('PASS: the upgrade and the rollback kept the test data and the fork assets')


if __name__ == '__main__':
    main()

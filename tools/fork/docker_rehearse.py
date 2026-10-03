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
import json
import os
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PROJECT, PORT = 'rotkibv-rehearsal', '18080'
API = f'http://127.0.0.1:{PORT}/api/1'
USER, PASSWORD = 'rbtest', secrets.token_urlsafe(18)
TRON_ACCOUNT = 'TQhqRHgEonKEYqudomS8243o3bejg8dt1d'  # synthetic: 41 and twenty a1 bytes
# seeded by the fork: the symbol of the asset and the symbol of its collection
FORK_ASSETS = {
    'TRX': ('TRX', 'TRX'),
    'tron/trc20:41a614f803b6fd780986a42c78ec9c7f77e6ded13c': ('USDT', 'USDT'),
}
VOLUME_DIRS = ('data', 'logs', 'config')


def stored(state, tron_events):
    """The test data is in the profile; `tron_events` is None for an image without TRON"""
    return 'rehearsal' in state['tags'] and state['kraken_events'] == 1 and (
        tron_events is None or
        (state.get('tron_accounts'), state.get('tron_events')) == ([TRON_ACCOUNT], tron_events)
    )


def kept(before, after):
    """Every value the old image reported is unchanged; an upgrade may raise the DB version"""
    return all(after.get(key) == value for key, value in before.items() if key != 'db_version')


def fork_assets(mappings):
    """The symbols of each fork asset and of its collection; None for a missing asset"""
    assets = mappings.get('assets') or {}
    collections = mappings.get('asset_collections') or {}
    return {
        identifier: (
            assets[identifier].get('symbol'),
            (collections.get(str(assets[identifier].get('collection_id'))) or {}).get('symbol'),
        ) if identifier in assets else None
        for identifier in FORK_ASSETS
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


def api(method, path, body=None):
    request = urllib.request.Request(
        f'{API}/{path}',
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={} if body is None else {'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}')


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


def supports_tron():
    return 'tron' in json.dumps(api('GET', 'blockchains/supported')[1]).lower()


def entries_found(**filters):
    return (api('POST', 'history/events', filters)[1].get('result') or {}).get('entries_found')


def all_events(label):
    status, body = api('POST', 'history/events', {})
    found = (body.get('result') or {}).get('entries_found')
    print(f'{label}: all history events HTTP {status} found {found} {said(body)!r}')


def add_tron_event(label, tx_ref):
    call(label, 'add TRON event', 'PUT', 'history/events', {
        'entry_type': 'tron event', 'tx_ref': tx_ref, 'timestamp': 1700000100000,
        'sequence_index': 0, 'location_label': TRON_ACCOUNT, 'event_type': 'receive',
        'event_subtype': 'none', 'asset': 'TRX', 'amount': '2',
        'user_notes': 'rehearsal TRON event',
    })
    print(f'{label}: TRON events', entries_found(location='tron'))


def add_tron_data(label):
    for what, address in (
            ('add TRON account', TRON_ACCOUNT),
            ('add it again', TRON_ACCOUNT),
            ('add an invalid address', TRON_ACCOUNT[:-1] + 'e'),
    ):
        call(label, what, 'PUT', 'blockchains/tron/accounts', {'accounts': [{'address': address}]})
    add_tron_event(label, 'ab' * 32)


def check_fork_assets(label):
    body = api('POST', 'assets/mappings', {'identifiers': list(FORK_ASSETS)})[1]
    found = fork_assets(body.get('result') or {})
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
    versions = api('GET', 'assets/updates')[1].get('result') or {}
    check(
        label,
        f'assets update HTTP {status} {said(body)!r} applied, now {versions}',
        status == 200 and bool(versions) and not update_offered(versions),
    )
    check_fork_assets(label)


def user_data(label):
    state = {
        'tags': sorted(api('GET', 'tags')[1].get('result') or {}),
        'kraken_events': entries_found(location='kraken'),
        'db_version': (api('GET', 'settings')[1].get('result') or {}).get('version'),
    }
    if supports_tron():
        accounts = api('GET', 'blockchains/tron/accounts')[1].get('result') or []
        state |= {
            'tron_accounts': [x['address'] for x in accounts],
            'tron_events': entries_found(location='tron'),
        }
    version = (api('GET', 'info')[1].get('result') or {}).get('version') or {}
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
        raise SystemExit(
            f'The {PROJECT} project already has containers. If no rehearsal is running, '
            f'remove them: docker compose -p {PROJECT} down -v',
        )
    try:
        with tempfile.TemporaryDirectory() as backup:
            # 1. the previous image creates a profile with some user data on fresh test volumes
            image_facts('old', container_id := up(old))
            call('old', 'create profile', 'PUT', 'users', {
                'name': USER, 'password': PASSWORD,
                'initial_settings': {'submit_usage_analytics': False},
            })
            call('old', 'add tag', 'PUT', 'tags', {
                'name': 'rehearsal', 'description': 'kept',
                'background_color': 'ffffff', 'foreground_color': '000000',
            })
            call('old', 'add event', 'PUT', 'history/events', {
                'entry_type': 'history event', 'timestamp': 1700000000000, 'sequence_index': 0,
                'group_identifier': 'rehearsal-1', 'location': 'kraken', 'event_type': 'receive',
                'event_subtype': 'none', 'asset': 'ETH', 'amount': '1.5',
                'user_notes': 'rehearsal event',
            })
            print('old: supports tron', old_has_tron := supports_tron())
            if old_has_tron:
                add_tron_data('old')
            before = user_data('old')
            check('old', 'test data stored', stored(before, 1 if old_has_tron else None))
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
            check('new', 'user data kept', kept(before, user_data('new')))
            print('new: supports tron', supports_tron())
            if old_has_tron:
                add_tron_event('new', 'cd' * 32)  # a second TRON event, written by the new image
            else:
                add_tron_data('new')
            with_tron = user_data('new')
            check('new', 'TRON data stored', stored(with_tron, 2 if old_has_tron else 1))
            assets_update('new')
            check('new', 'user data kept after the assets update', user_data('new') == with_tron)
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
            except (subprocess.CalledProcessError, OSError, ValueError) as e:
                print(f'downgrade without restore: failed, {e}')
                print(compose(old, 'logs', '--tail', '20'))

            # 5. rollback: the previous image with its matching backup on new volumes
            compose(old, 'down', '-v')
            compose(old, 'create')
            container_id = compose(old, 'ps', '-aq', 'rotki')
            for directory in VOLUME_DIRS:
                docker('cp', f'{backup}/{directory}/.', f'{container_id}:/{directory}')
            image_facts('rollback', up(old))
            check('rollback', 'profile opens', login('rollback'))
            check('rollback', 'user data kept', user_data('rollback') == before)
            all_events('rollback')
            logout()
    finally:
        # 6. remove the rehearsal containers, volumes and backup
        compose(old, 'down', '-v')
        print('cleanup: done')
    print('PASS: the upgrade and the rollback kept the test data and the fork assets')


if __name__ == '__main__':
    main()

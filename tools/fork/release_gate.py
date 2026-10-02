"""Require a reviewed PR and passing main CI before publishing a fork image."""
# Trusted CI tools, argv only; never invoke a shell.
# ruff: noqa: S404, S603, S607
import json
import os
import re
import subprocess
from operator import itemgetter


def eligible_pull(pull, sha):
    return (
        pull['merged_at'] is not None
        and pull['base']['ref'] == 'main'
        and pull['merge_commit_sha'] == sha
    )


def reviewed(statuses, owner):
    # GitHub returns newest statuses first; a later rejection overrides approval.
    status = next((s for s in statuses if s['context'] == 'Independent review'), None)
    return (
        status is not None
        and status['state'] == 'success'
        and status['creator']['login'] == owner
    )


def ci_passed(runs, sha):
    runs = [
        r for r in runs
        if r['head_sha'] == sha and r['head_branch'] == 'main' and r['event'] == 'push'
    ]
    return bool(runs) and max(runs, key=itemgetter('id'))['conclusion'] == 'success'


def api(path):
    return json.loads(subprocess.check_output(['gh', 'api', path], text=True))


def main():
    repo = os.environ['GITHUB_REPOSITORY']
    tag = os.environ['GITHUB_REF_NAME']
    if re.fullmatch(r'v\d+\.\d+\.\d+-bv\.[1-9]\d*', tag) is None:
        raise SystemExit('Invalid fork release tag')
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['git', 'merge-base', '--is-ancestor', sha, 'origin/main'], check=True)
    runs = api(
        f'repos/{repo}/actions/workflows/fork-ci.yml/runs'
        f'?head_sha={sha}&event=push&per_page=100',
    )
    if not ci_passed(runs['workflow_runs'], sha):
        raise SystemExit('The latest main CI for this commit must pass before tagging')
    pulls = api(f'repos/{repo}/commits/{sha}/pulls?per_page=100')
    for pull in pulls:
        if eligible_pull(pull, sha):
            statuses = api(f"repos/{repo}/commits/{pull['head']['sha']}/statuses?per_page=100")
            if reviewed(statuses, repo.split('/')[0]):
                print(f"Release approved: {sha}, reviewed PR #{pull['number']}")
                return
    raise SystemExit('A merged PR with independent review of its final head is required')


if __name__ == '__main__':
    main()

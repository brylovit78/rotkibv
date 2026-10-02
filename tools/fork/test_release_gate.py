"""Fail closed when a release has stale review, failed CI, or no merged PR."""
# Stdlib runner keeps delivery checks independent of application dependencies.
# ruff: noqa: PT009
import unittest

from release_gate import ci_passed, eligible_pull, reviewed


class ReleaseGateTests(unittest.TestCase):
    def test_review_is_for_latest_status_and_trusted_owner(self):
        approved = {
            'context': 'Independent review', 'state': 'success', 'creator': {'login': 'owner'},
        }
        rejected = dict(approved, state='failure')
        self.assertTrue(reviewed([approved], 'owner'))
        self.assertFalse(reviewed([approved], 'outsider'))
        self.assertFalse(reviewed([rejected, approved], 'owner'))
        self.assertFalse(reviewed([], 'owner'))

    def test_release_requires_merged_main_pr_for_this_commit(self):
        pull = {'merged_at': '2026-10-02', 'base': {'ref': 'main'}, 'merge_commit_sha': 'abc'}
        self.assertTrue(eligible_pull(pull, 'abc'))
        self.assertFalse(eligible_pull(pull, 'old'))
        self.assertFalse(eligible_pull(dict(pull, merged_at=None), 'abc'))
        self.assertFalse(eligible_pull(dict(pull, base={'ref': 'develop'}), 'abc'))

    def test_ci_must_be_latest_main_push_for_exact_commit(self):
        run = {
            'id': 1, 'head_sha': 'abc', 'head_branch': 'main',
            'event': 'push', 'conclusion': 'success',
        }
        self.assertTrue(ci_passed([run], 'abc'))
        self.assertFalse(ci_passed([run], 'other'))
        self.assertFalse(ci_passed([dict(run, event='pull_request')], 'abc'))
        for result in ('failure', 'cancelled', 'skipped', None):
            self.assertFalse(ci_passed([run, dict(run, id=2, conclusion=result)], 'abc'))
        self.assertFalse(ci_passed([], 'abc'))


if __name__ == '__main__':
    unittest.main()

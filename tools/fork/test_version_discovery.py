"""Fork image tags must not become Python package versions."""
# Trusted local Git commands, no shell; stdlib keeps delivery checks standalone.
# ruff: noqa: PT009, S404, S603, S607
import shlex
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


class VersionDiscoveryTests(unittest.TestCase):
    def test_fork_tags_preserve_upstream_and_dirty_versions(self):
        config = tomllib.loads((Path(__file__).resolve().parents[2] / 'pyproject.toml').read_text())
        command = shlex.split(config['tool']['setuptools_scm']['git_describe_command'])
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=directory, text=True).strip()

            git('init', '--quiet')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            file = Path(directory) / 'tracked'
            file.write_text('upstream')
            git('add', 'tracked')
            git('commit', '--quiet', '-m', 'Upstream')
            git('tag', 'v1.44.0')
            file.write_text('fork')
            git('commit', '--quiet', '-am', 'Fork')
            git('tag', 'v1.44.0-bv.1')
            git('tag', 'v1.44.0-bv.2')
            version = subprocess.check_output(command, cwd=directory, text=True).strip()
            self.assertRegex(version, r'^v1\.44\.0-1-g[0-9a-f]+$')
            file.write_text('dirty')
            self.assertEqual(
                subprocess.check_output(command, cwd=directory, text=True).strip(),
                version + '-dirty',
            )

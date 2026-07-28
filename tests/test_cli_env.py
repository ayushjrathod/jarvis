"""The `claude` subprocess environment.

The quick and agentic paths both run `claude -p` on the **Claude Code
subscription login**. The CLI prefers an API key over that login whenever one
is in the environment, and says so:

    claude.ai connectors are disabled because ANTHROPIC_API_KEY or another
    auth source is set and takes precedence over your claude.ai login

so a stray key doesn't supplement the subscription, it *replaces* it — an
unfunded key then breaks every run (observed 2026-07-27), and a funded one
would silently bill each ~18k-token cold start to API credit. The dispatcher
never sets these variables itself, but a hand-run dispatcher can inherit them
from the operator's shell, so they are stripped at the boundary.
"""

import os
import unittest
from unittest import mock

from dispatcher import runner


class TestCliEnv(unittest.TestCase):
    def test_credentials_are_removed(self):
        cfg = mock.Mock(claude_config_dir="/home/ayra/.claude-per")
        with mock.patch.dict("os.environ",
                             {"ANTHROPIC_API_KEY": "sk-x",
                              "ANTHROPIC_AUTH_TOKEN": "tok",
                              "PATH": "/usr/bin"}, clear=True):
            env = runner.cli_env(cfg)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertEqual(env["PATH"], "/usr/bin")            # rest inherited
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/home/ayra/.claude-per")

    def test_no_config_dir_leaves_it_unset(self):
        cfg = mock.Mock(claude_config_dir=None)
        with mock.patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            self.assertNotIn("CLAUDE_CONFIG_DIR", runner.cli_env(cfg))

    def test_os_environ_is_not_mutated(self):
        cfg = mock.Mock(claude_config_dir=None)
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-x"}, clear=True):
            runner.cli_env(cfg)
            self.assertIn("ANTHROPIC_API_KEY", os.environ)   # only the copy is filtered

    def test_clean_environment_is_untouched(self):
        cfg = mock.Mock(claude_config_dir="/cfg")
        with mock.patch.dict("os.environ", {"HOME": "/home/ayra"}, clear=True):
            env = runner.cli_env(cfg)
        self.assertEqual(env, {"HOME": "/home/ayra", "CLAUDE_CONFIG_DIR": "/cfg"})


if __name__ == "__main__":
    unittest.main()

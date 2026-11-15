"""Unit tests for agents_bridge configuration, path isolation, MCP protocol, and examples."""
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_fresh_mcp(env_updates):
    with patch.dict(os.environ, env_updates, clear=False):
        import agents_bridge.agents_mcp as mod
        return importlib.reload(mod)


class AgentsBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        # Reload with clean defaults so other test modules aren't affected.
        for key in ("AGENT_DECK_HOME", "AGENTS_ALLOWED_ROOTS", "AGENTS_JOB_DIR", "AGENTS_DEFAULTS_FILE"):
            os.environ.pop(key, None)
        import agents_bridge.agents_mcp as mod
        importlib.reload(mod)

    def test_allowed_roots_env_and_default_home(self):
        ws1 = self.root / "ws1"
        ws2 = self.root / "ws2"
        outside = self.root / "outside"
        for d in (ws1, ws2, outside):
            d.mkdir()
        sub = ws1 / "sub"
        sub.mkdir()

        mod = load_fresh_mcp({
            "AGENT_DECK_HOME": str(self.root / "deck"),
            "AGENTS_ALLOWED_ROOTS": f"{ws1}{os.pathsep}{ws2}",
        })
        self.assertEqual(mod.ALLOWED_ROOTS, [ws1, ws2])
        self.assertEqual(mod.check_cwd(str(sub)), sub)
        self.assertEqual(mod.check_cwd(str(ws2)), ws2)
        with self.assertRaises(ValueError):
            mod.check_cwd(str(outside))
        with self.assertRaises(ValueError):
            mod.check_cwd(str(self.root / "nonexistent"))

        with patch.dict(os.environ, {"AGENTS_ALLOWED_ROOTS": ""}):
            mod_default = load_fresh_mcp({"AGENT_DECK_HOME": str(self.root / "deck")})
            self.assertEqual(mod_default.ALLOWED_ROOTS, [Path.home().resolve()])

    def test_shared_agent_deck_home_with_aw_core(self):
        deck = self.root / "shared-deck"
        mcp = load_fresh_mcp({"AGENT_DECK_HOME": str(deck)})
        with patch.dict(os.environ, {"AGENT_DECK_HOME": str(deck)}):
            import aw.core as aw_core
            aw_core = importlib.reload(aw_core)
            self.assertEqual(mcp.DECK_HOME, deck)
            self.assertEqual(mcp.JOB_DIR, aw_core.DEFAULT_JOBS)
            self.assertEqual(mcp.LOG_DIR, deck / "logs")
            self.assertEqual(mcp.DEFAULTS, deck / "agents_defaults.json")
            self.assertEqual(aw_core.DEFAULT_BOARD, deck / "board.json")

        # Reload aw.core back to clean state
        os.environ.pop("AGENT_DECK_HOME", None)
        importlib.reload(aw_core)

    def test_defaults_init_empty_and_approval_allowlist_checks(self):
        deck = self.root / "deck"
        mod = load_fresh_mcp({"AGENT_DECK_HOME": str(deck)})
        res = mod.agent_defaults_get({})
        self.assertEqual(Path(res["path"]), deck / "agents_defaults.json")
        self.assertEqual(res["defaults"]["antigravity"]["default_model"], "")
        self.assertEqual(res["defaults"]["codex"]["default_model"], "")
        self.assertEqual(res["defaults"]["codex"]["default_effort"], "")

        refused = mod.agent_defaults_set({"agent": "codex", "model": "m1", "user_approval": "   "})
        self.assertIn("refused", refused["error"])

        data = json.loads((deck / "agents_defaults.json").read_text(encoding="utf-8"))
        data["codex"]["allowed_models"] = ["allowed-m"]
        (deck / "agents_defaults.json").write_text(json.dumps(data), encoding="utf-8")

        disallowed = mod.agent_defaults_set({"agent": "codex", "model": "other-m", "user_approval": "ok"})
        self.assertIn("refused", disallowed["error"])

        ok = mod.agent_defaults_set({"agent": "codex", "model": "allowed-m", "user_approval": "switch to allowed-m"})
        self.assertEqual(ok["changes"]["default_model"]["new"], "allowed-m")
        self.assertTrue((deck / "logs" / "agents_defaults_changes.jsonl").is_file())

    def test_mcp_initialize_and_tools_list_have_no_private_strings(self):
        mod = load_fresh_mcp({"AGENT_DECK_HOME": str(self.root / "deck")})
        captured = []
        with patch.object(mod.sys.stdout, "write", side_effect=captured.append), patch.object(mod.sys.stdout, "flush"):
            mod.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            mod.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        init_msg = json.loads(captured[0])
        tools_msg = json.loads(captured[1])
        self.assertEqual(init_msg["result"]["serverInfo"]["name"], "agents-bridge")
        tools_blob = json.dumps(tools_msg["result"]["tools"], ensure_ascii=False)
        for forbidden in ("/Volumes/", "VM", "00_"):
            self.assertNotIn(forbidden, tools_blob)
        self.assertIn("AGENTS_ALLOWED_ROOTS", tools_blob)
        self.assertIn("AGENT_DECK_HOME", tools_blob)

    def test_examples_are_valid_against_runtime_loaders(self):
        from aw import core
        board_path = REPO_ROOT / "examples" / "board.example.json"
        board, err = core.load_board(board_path)
        self.assertIsNone(err)
        self.assertEqual(board["schema"], 1)
        self.assertGreaterEqual(len(board["decisions"]), 2)
        self.assertGreaterEqual(len(board["progress"]), 2)

        defaults_path = REPO_ROOT / "examples" / "agents_defaults.example.json"
        mod = load_fresh_mcp({
            "AGENT_DECK_HOME": str(self.root / "deck"),
            "AGENTS_DEFAULTS_FILE": str(defaults_path),
        })
        loaded = mod.load_defaults()
        self.assertEqual(loaded["antigravity"]["default_model"], "")
        self.assertEqual(loaded["codex"]["default_model"], "")


if __name__ == "__main__":
    unittest.main()

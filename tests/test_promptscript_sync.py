"""Tests for PromptScript output synchronization."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_promptscript_outputs.py"


def load_sync_module():
    """Load the synchronization script as a test module."""
    spec = importlib.util.spec_from_file_location("sync_promptscript_outputs", SYNC_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load synchronization script")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PromptScriptSyncTest(unittest.TestCase):
    """Verify managed resources cannot remain stale."""

    def test_stale_codex_interface_is_reported_and_removed(self):
        sync = load_sync_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            stale = destination / "skill" / "agents" / "openai.yaml"
            stale.parent.mkdir(parents=True)
            stale.write_text("interface: {}\n", encoding="utf-8")

            errors = sync.sync_tree(root, source, destination, True, "skills")

            self.assertEqual(
                errors,
                [f"skills: stale generated file {stale}"],
            )
            sync.sync_tree(root, source, destination, False, "skills")
            self.assertFalse(stale.exists())

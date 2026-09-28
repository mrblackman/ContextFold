#!/usr/bin/env python3
"""
test_ipcf_conformance.py — IPCF-1.1 Normative Conformance Test Suite
Tests compliance with the 12 normative requirements of CONFORMANCE.md.

Zero external dependencies. Python 3.10+ unittest.
All tests run with filesystem isolation (tempfile.TemporaryDirectory + os.chdir).
"""

import unittest
import tempfile
import shutil
import os
import sys
import json
import time

# Ensure contextfold is importable from repository root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import contextfold


class MockHarnessContext:
    """
    Simulates an active Agent Harness / IDE Runtime environment (Z-2, Y-6, CONF-08, CONF-13, D-7).
    Bridges contextfold reference engine with LLM prompt context window simulation.
    """
    def __init__(self, initial_tokens: int = 8100):
        self.baseline_tokens = initial_tokens
        self.active_prompt_tokens = initial_tokens
        self.hydrated_nodes = []
        self.hydrated_node_tokens = {}

    def read_cold_node_ui(self, step_id: int) -> dict:
        """
        CONF-08: Simulates developer inspecting cold storage in an IDE side drawer.
        Zero token injection into active prompt.
        """
        node_path = contextfold.cold_node_path(step_id)
        with open(node_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def dispatch_turn(self, user_prompt: str) -> list:
        """
        CONF-13: Simulates pre_turn_dispatch Software MMU passive interception.
        Calls actual engine and increases token estimate from all hydrated nodes (multi-intent RGSC).
        """
        matches = contextfold.cmd_scan(user_prompt)
        if matches:
            for step_id, _, _ in matches:
                node = contextfold.cmd_hydrate(step_id)
                self.hydrated_nodes.append(step_id)
                self.hydrated_node_tokens[step_id] = node.get("token_estimate", 10)
            self.active_prompt_tokens = self.baseline_tokens + sum(self.hydrated_node_tokens.values())
        return matches

    def end_turn(self) -> None:
        """
        CONF-07: Simulates post_turn_response eviction.
        Evicts all hydrated nodes and recalculates active prompt tokens based on eviction result (D-7).
        """
        for step_id in list(self.hydrated_nodes):
            res = contextfold.cmd_evict(step_id)
            if res and res.get("step_id") == step_id:
                self.hydrated_node_tokens.pop(step_id, None)
        self.hydrated_nodes.clear()
        self.active_prompt_tokens = self.baseline_tokens + sum(self.hydrated_node_tokens.values())


class TestIPCFConformance(unittest.TestCase):
    """
    Normative 12 Core Conformance Tests for IPCF-1.1.
    """

    def setUp(self):
        """Isolate each test in a clean temporary directory (Z-2)."""
        self.orig_cwd = os.getcwd()
        self.test_dir = tempfile.mkdtemp(prefix="ipcf_test_")
        os.chdir(self.test_dir)
        contextfold.ensure_dirs()

    def tearDown(self):
        """Restore original CWD and clean up temporary directory."""
        os.chdir(self.orig_cwd)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_conf_01_lossless_storage(self):
        """
        CONF-01: Folded turn payloads written to disk must preserve 100% of original bytes.
        """
        raw_text = (
            "SELECT * FROM accounts WHERE tenant_id = 't-992'\n"
            "ORDER BY created_at DESC;\n"
            "Unicode test: Türkçe karakterler: çığöşü ÇİĞÖŞÜ\n"
            "Special symbols: <>$&\"'\t\r\n"
        )
        step_id = 1
        summary = "Account query with unicode test"
        contextfold.cmd_fold(step_id, summary, raw_content=raw_text)

        node_path = contextfold.cold_node_path(step_id)
        self.assertTrue(os.path.exists(node_path))

        with open(node_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["raw_content"], raw_text, "CONF-01 failed: content altered or truncated")
        self.assertEqual(len(data["raw_content"].encode("utf-8")), len(raw_text.encode("utf-8")))

    def test_conf_02_payload_round_trip(self):
        """
        CONF-02: payload_sha256(source) == payload_sha256(stored) == payload_sha256(hydrated).
        Tested with content under hydration token cap (Z-6).
        """
        raw_text = "Configuration updated: database pool size set to 25."
        step_id = 2
        summary = "Database pool configuration"

        expected_hash = contextfold.sha256_of_str(raw_text)
        contextfold.cmd_fold(step_id, summary, raw_content=raw_text)

        # 1. Stored in cold node
        with open(contextfold.cold_node_path(step_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["payload_sha256"], expected_hash)

        # 2. Retrieved via hydration
        hydrated = contextfold.cmd_hydrate(step_id)
        self.assertEqual(hydrated["payload_sha256"], expected_hash)
        self.assertEqual(contextfold.sha256_of_str(hydrated["raw_content"]), expected_hash)

    def test_conf_03_deterministic_addressing(self):
        """
        CONF-03: Given identical conversation history, the extracted index must be bit-for-bit identical.
        """
        turns = [
            (10, "Initial project setup", "$ npm init -y\nCreated package.json"),
            (11, "Installed dependencies", "$ npm install express pg redis\nAdded 3 packages"),
            (12, "Configured database connection", "Connecting to postgres on port 5432"),
        ]

        # First run
        for s_id, s_sum, s_raw in turns:
            contextfold.cmd_fold(s_id, s_sum, raw_content=s_raw)
        with open(contextfold.INDEX_FILE, "rb") as f:
            index_run_1 = f.read()

        # Clean directory and rerun identical history
        shutil.rmtree(".contextfold")
        contextfold.ensure_dirs()

        for s_id, s_sum, s_raw in turns:
            contextfold.cmd_fold(s_id, s_sum, raw_content=s_raw)
        with open(contextfold.INDEX_FILE, "rb") as f:
            index_run_2 = f.read()

        # Parse and compare normalized nodes structure (timestamps might differ by seconds if not mocked)
        idx1 = json.loads(index_run_1.decode("utf-8"))
        idx2 = json.loads(index_run_2.decode("utf-8"))

        for k in idx1["nodes"]:
            self.assertIn(k, idx2["nodes"])
            node1 = idx1["nodes"][k]
            node2 = idx2["nodes"][k]
            self.assertEqual(node1["payload_sha256"], node2["payload_sha256"])
            self.assertEqual(node1["projection_sha256"], node2["projection_sha256"])
            self.assertEqual(node1["identifiers"], node2["identifiers"])
            self.assertEqual(node1["commands"], node2["commands"])

    def test_conf_04_temporal_disambiguation(self):
        """
        CONF-04: When an entity evolves across turns, recall must rank candidates chronologically (D-1, D-2).
        Uses canonical DEMO_TURNS fixture directly.
        PostgreSQL Port Scenario: Port 5433 -> 5434 -> 5433 (with Step 499 as single-term related step).
        """
        for step_id, summary, raw in contextfold.DEMO_TURNS:
            contextfold.cmd_fold(step_id, summary, raw_content=raw)

        candidates = contextfold.cmd_recall("PostgreSQL port")
        self.assertGreaterEqual(len(candidates), 3)

        # Validate primary tier ordering (chronological latest first)
        self.assertEqual(candidates[0]["step_id"], 137, "CONF-04: Latest chronological state (step 137) was not ranked first")
        self.assertEqual(candidates[0]["status"], "latest_chronological", "CONF-04: Step 137 did not receive 'latest_chronological' status")

        self.assertEqual(candidates[1]["step_id"], 91, "CONF-04: Superseded state (step 91) was not ranked second")
        self.assertEqual(candidates[1]["status"], "superseded", "CONF-04: Step 91 did not receive 'superseded' status")

        self.assertEqual(candidates[2]["step_id"], 37, "CONF-04: Historical initial state (step 37) was not ranked third")
        self.assertEqual(candidates[2]["status"], "historical", "CONF-04: Step 37 did not receive 'historical' status")

        # Verify Step 499 is in secondary tier with 'related' status (D-1, D-2)
        step_499_matches = [c for c in candidates if c["step_id"] == 499]
        if step_499_matches:
            self.assertEqual(step_499_matches[0]["status"], "related", "D-1 / CONF-04: Low-coverage Step 499 was not labeled 'related'")
            self.assertLess(step_499_matches[0]["coverage"], candidates[0]["coverage"])

        # V-1: Verify noisy query ("a port postgresql") preserves tiering and ranking
        noisy_candidates = contextfold.cmd_recall("a port postgresql")
        self.assertEqual(noisy_candidates[0]["step_id"], 137, "V-1: Step 137 should remain first under noisy query")
        self.assertEqual(noisy_candidates[0]["status"], "latest_chronological")
        self.assertEqual(noisy_candidates[1]["step_id"], 91)
        self.assertEqual(noisy_candidates[1]["status"], "superseded")
        self.assertEqual(noisy_candidates[2]["step_id"], 37)
        self.assertEqual(noisy_candidates[2]["status"], "historical")

        # V-1 Substring Trap: 3+ character token ('res') present as substring in 91 ('resolution') and 137 ('restored'), but not 37
        # Exact term semantics ensure Step 37 remains in primary tier
        trap_candidates = contextfold.cmd_recall("res port postgresql")
        self.assertEqual(trap_candidates[0]["step_id"], 137, "V-1 Substring Trap: Step 137 should remain first")
        self.assertEqual(trap_candidates[0]["status"], "latest_chronological")
        self.assertEqual(trap_candidates[1]["step_id"], 91)
        self.assertEqual(trap_candidates[1]["status"], "superseded")
        self.assertEqual(trap_candidates[2]["step_id"], 37, "V-1 Substring Trap: Step 37 must not drop from primary tier")
        self.assertEqual(trap_candidates[2]["status"], "historical")

        # V-2: Demo scenario software MMU scan ranking (matches[0][0] == 137)
        scan_matches = contextfold.cmd_scan("What port did we configure for PostgreSQL?")
        self.assertTrue(len(scan_matches) > 0, "V-2: MMU scan should find matches for demo PostgreSQL port query")
        self.assertEqual(scan_matches[0][0], 137, "V-2: MMU scan must rank latest chronological step 137 first")

    def test_conf_05_exact_verbatim_retrieval(self):
        """
        CONF-05: Historical payload retrieved from cold storage must bypass LLM re-interpretation.
        Direct I/O stream from disk.
        """
        raw_diff = "diff --git a/server.js b/server.js\n- const port = 3000;\n+ const port = 8080;"
        contextfold.cmd_fold(55, "Server port update diff", raw_content=raw_diff)

        hydrated = contextfold.cmd_hydrate(55)
        self.assertEqual(hydrated["raw_content"], raw_diff, "CONF-05: Retrieved content does not match verbatim disk bytes")

    def test_conf_06_bounded_rehydration_cap(self):
        """
        CONF-06: On-demand hydration cannot exceed max_rehydration_tokens.
        W-1: Bounded rehydration enforced at return level.
        N-1 / D-8: Prompt payload bounded in 'content', raw_content preserves 100% disk bytes.
        """
        policy = contextfold.load_policy()
        policy["max_rehydration_tokens_per_call"] = 50  # Cap at 50 tokens (~200 chars)
        contextfold.atomic_write_json(contextfold.POLICY_FILE, policy)

        large_payload = "A" * 1000  # 1000 chars = ~250 tokens
        contextfold.cmd_fold(60, "Large payload step", raw_content=large_payload)

        # Hydrate should succeed and apply truncation cap to content while preserving full raw_content (N-1)
        hydrated = contextfold.cmd_hydrate(60)
        self.assertIsNotNone(hydrated)
        self.assertTrue(hydrated.get("truncated"), "CONF-06 failed: truncated flag is not True")
        self.assertEqual(hydrated["token_estimate"], 50, "CONF-06 failed: token_estimate exceeded cap")
        self.assertEqual(len(hydrated["content"]), 200, "CONF-06 failed: content was not truncated to 200 chars")
        # N-1: raw_content is preserved lossless (1000 chars) and matches payload_sha256
        self.assertEqual(len(hydrated["raw_content"]), 1000, "CONF-06 / N-1: raw_content was incorrectly mutated")
        self.assertEqual(contextfold.sha256_of_str(hydrated["raw_content"]), hydrated["payload_sha256"], "CONF-02 / N-1: raw_content hash mismatch")

    def test_conf_07_enforced_eviction_cycle(self):
        """
        CONF-07: Recalled node tokens MUST be evicted from the prompt on the subsequent turn.
        Also tests multiple eviction events for the same node (Z-5).
        """
        contextfold.cmd_fold(70, "API Gateway config", "Routing rules configured for gateway")
        session = MockHarnessContext(initial_tokens=8100)

        # Turn 1: Hydrate step 70
        session.dispatch_turn("gateway routing")
        self.assertGreater(session.active_prompt_tokens, 8100, "Prompt tokens did not increase after hydration")

        # Turn 1 End: Evict
        session.end_turn()
        self.assertEqual(session.active_prompt_tokens, 8100, "Prompt tokens did not return to baseline after eviction")

        # Turn 2: Hydrate step 70 again
        session.dispatch_turn("gateway routing")
        self.assertGreater(session.active_prompt_tokens, 8100)
        session.end_turn()
        self.assertEqual(session.active_prompt_tokens, 8100)

        # Verify eviction log has recorded both events (Z-5)
        eviction_log = contextfold.load_eviction_log()
        evictions_for_70 = [e for e in eviction_log["evicted"] if e["step_id"] == 70]
        self.assertGreaterEqual(len(evictions_for_70), 2, "CONF-07 / Z-5: Successive evictions not logged as separate events")

    def test_conf_08_dual_projection_isolation(self):
        """
        [STRUCTURAL / DOCUMENTARY TEST] CONF-08: Viewing cold nodes in Developer UI side-drawer must NOT inject tokens into LLM prompt (W-8).
        """
        raw_dump = "DEBUG LOG: " + ("x" * 4000)
        contextfold.cmd_fold(80, "Large debug trace dump", raw_content=raw_dump)
        session = MockHarnessContext(initial_tokens=8100)

        # Developer opens UI side-drawer to inspect step 80
        ui_node = session.read_cold_node_ui(80)
        self.assertEqual(ui_node["step_id"], 80)
        self.assertEqual(len(ui_node["raw_content"]), len(raw_dump))

        # Model prompt token count MUST be completely unaffected
        self.assertEqual(session.active_prompt_tokens, 8100, "CONF-08 failed: UI inspection leaked tokens into prompt")

    def test_conf_09_tamper_detection(self):
        """
        CONF-09: Modification of the cold node file on disk MUST be detected via artifact_sha256 mismatch.
        """
        contextfold.cmd_fold(90, "Security settings review", "Firewall enabled on port 22 and 443")
        node_path = contextfold.cold_node_path(90)

        # Tamper with file: change 1 semantic content character
        data = contextfold.load_json(node_path)
        data["summary"] = "Tampered summary!"
        contextfold.atomic_write_json(node_path, data)

        # Hydrate must raise IntegrityCheckError
        with self.assertRaises(contextfold.IntegrityCheckError):
            contextfold.cmd_hydrate(90)

        # Validate must report failure
        valid = contextfold.cmd_validate(exit_on_error=False)
        self.assertFalse(valid, "CONF-09: Tampered file was not detected by cmd_validate")

    def test_conf_10_explicit_failure_mode(self):
        """
        CONF-10: Querying a non-existent step must fail explicitly (NodeNotFoundError).
        """
        with self.assertRaises(contextfold.NodeNotFoundError):
            contextfold.cmd_hydrate(99999)

    def test_conf_11_deterministic_replay(self):
        """
        CONF-11: Given identical history and policy, projection_sha256 MUST be bit-for-bit identical.
        Runtime metadata (folded_at) MAY differ without affecting projection_sha256.
        """
        step_id = 110
        summary = "Deployment pipeline setup"
        raw_text = "$ docker build -t app:v1 .\nDeployed app:v1 to staging."
        files = ["Dockerfile", "docker-compose.yml"]
        commands = ["docker build -t app:v1 ."]
        errors = []

        # Run A
        node_a = contextfold.cmd_fold(
            step_id, summary, raw_content=raw_text, files=files, commands=commands, errors=errors
        )
        proj_a = node_a["projection_sha256"]

        # Run B in another isolated directory with identical content
        tmp_dir_b = tempfile.mkdtemp(prefix="ipcf_replay_")
        orig_cwd = os.getcwd()
        try:
            os.chdir(tmp_dir_b)
            contextfold.ensure_dirs()
            time.sleep(0.01)  # Ensure timestamps differ

            node_b = contextfold.cmd_fold(
                step_id, summary, raw_content=raw_text, files=files, commands=commands, errors=errors
            )
            proj_b = node_b["projection_sha256"]

            self.assertEqual(proj_a, proj_b, "CONF-11: projection_sha256 differs across runs for identical inputs")
            self.assertEqual(node_a["projection_version"], "ipcf-projection-v2", "CONF-11 / V-4: projection_version must be ipcf-projection-v2")
            self.assertEqual(node_b["projection_version"], "ipcf-projection-v2", "CONF-11 / V-4: projection_version must be ipcf-projection-v2")
        finally:
            os.chdir(orig_cwd)
            shutil.rmtree(tmp_dir_b, ignore_errors=True)

        # W-4b: Verify cross-platform Unicode normalization determinism (NFC vs NFD)
        import unicodedata
        nfc_summary = "Café service configuration"
        nfd_summary = unicodedata.normalize("NFD", nfc_summary)
        nfc_text = "café port 5433 connection"
        nfd_text = unicodedata.normalize("NFD", nfc_text)

        tmp_dir_nfd = tempfile.mkdtemp(prefix="ipcf_nfc_nfd_")
        try:
            os.chdir(tmp_dir_nfd)
            contextfold.ensure_dirs()
            node_nfc = contextfold.cmd_fold(201, nfc_summary, raw_content=nfc_text)

            shutil.rmtree(".contextfold")
            contextfold.ensure_dirs()
            node_nfd = contextfold.cmd_fold(201, nfd_summary, raw_content=nfd_text)

            self.assertEqual(node_nfc["identifiers"], node_nfd["identifiers"], "W-4b: NFC and NFD extracted identifiers mismatch")
            self.assertEqual(
                node_nfc["projection_sha256"],
                node_nfd["projection_sha256"],
                "CONF-11 / W-4b: projection_sha256 differs between NFC and NFD representations of identical text"
            )
        finally:
            os.chdir(orig_cwd)
            shutil.rmtree(tmp_dir_nfd, ignore_errors=True)

    def test_conf_12_inconsistent_state_detection(self):
        """
        CONF-12: Detect broken states (Scenario A: Orphaned node, Scenario B: Broken reference, Scenario C: Corrupted/truncated file).
        """
        # Scenario A: Orphaned node on disk not in index
        orphan_path = contextfold.cold_node_path(999)
        orphan_data = {
            "ipcf_version": "1.1",
            "projection_version": contextfold.PROJECTION_VERSION,
            "step_id": 999,
            "session_id": "test",
            "folded_at": contextfold._iso_now(),
            "summary": "Orphan node",
            "raw_content": "Orphan content",
            "normalized_content": "Orphan content",
            "payload_sha256": contextfold.sha256_of_str("Orphan content"),
            "projection_sha256": "0" * 64,
            "artifact_sha256": "0" * 64,
            "identifiers": ["orphan"],
            "files": [],
            "commands": [],
            "errors": [],
            "token_estimate": 10,
            "sanitized": False,
            "status": "cold"
        }
        contextfold.atomic_write_json(orphan_path, orphan_data)

        # Hydrate must raise OrphanedNodeError (W-5)
        with self.assertRaises(contextfold.OrphanedNodeError):
            contextfold.cmd_hydrate(999)

        # Validate should detect Scenario A
        valid = contextfold.cmd_validate(exit_on_error=False)
        self.assertFalse(valid, "CONF-12 Scenario A: Orphaned cold node was not detected")

        # Clean up orphan
        os.unlink(orphan_path)

        # Scenario B: Index references missing file
        index = contextfold.load_index()
        index["nodes"]["888"] = {
            "step_id": 888,
            "projection_version": contextfold.PROJECTION_VERSION,
            "summary": "Ghost step",
            "file_path": contextfold.cold_node_path(888).replace("\\", "/"),
            "payload_sha256": "0" * 64,
            "projection_sha256": "0" * 64,
            "artifact_sha256": "0" * 64,
            "folded_at": contextfold._iso_now(),
            "evicted": False
        }
        contextfold.save_index(index)

        # Hydrate should raise BrokenReferenceError
        with self.assertRaises(contextfold.BrokenReferenceError):
            contextfold.cmd_hydrate(888)

        # Validate should also fail
        valid_b = contextfold.cmd_validate(exit_on_error=False)
        self.assertFalse(valid_b, "CONF-12 Scenario B: Broken reference was not detected")

        # Clean up Scenario B broken reference to isolate Scenario C (N-2)
        del index["nodes"]["888"]
        contextfold.save_index(index)

        # Scenario C: Corrupted/truncated JSON written mid-byte (W-2)
        corrupt_path = contextfold.cold_node_path(777)
        with open(corrupt_path, "w", encoding="utf-8") as f:
            f.write('{"step_id": 777, "raw_content": "Truncated mid-byte...')  # invalid JSON

        index["nodes"]["777"] = {
            "step_id": 777,
            "projection_version": contextfold.PROJECTION_VERSION,
            "summary": "Corrupted step",
            "file_path": corrupt_path.replace("\\", "/"),
            "payload_sha256": "0" * 64,
            "projection_sha256": "0" * 64,
            "artifact_sha256": "0" * 64,
            "folded_at": contextfold._iso_now(),
            "evicted": False
        }
        contextfold.save_index(index)

        # Hydrate must raise IntegrityCheckError (not raw JSONDecodeError)
        with self.assertRaises(contextfold.IntegrityCheckError):
            contextfold.cmd_hydrate(777)

        # Validate must handle gracefully and report error without crashing (isolated N-2 test)
        valid_c = contextfold.cmd_validate(exit_on_error=False)
        self.assertFalse(valid_c, "CONF-12 Scenario C: Corrupted JSON was not detected")

    def test_conf_13_passive_mmu_unicode(self):
        """
        Candidate CONF-13 / D-10: Passive Recall Interception (Software MMU) with Unicode and Turkish Support.
        Verifies:
          1. Code identifiers starting with underscore ('__init__', '_private_var') are extracted and indexed (D-3a).
          2. Non-ASCII Turkish entities ('sipariş_tablosu', 'çığöşü') are extracted and matched via MMU (N-3).
          3. Case-folding and Unicode normalization works across NFC/NFD and Turkish I/i ('İstanbul' -> 'istanbul') (D-4).
          4. MockHarnessContext.dispatch_turn transparently hydrates the matching cold node without LLM intervention.
          5. Single-path verification for dotted-I (U+0307) and NFD entities (V-2).
          6. Turkish stopword elision verification (V-6).
        """
        step_id = 150
        summary = "İstanbul için veri merkezi sipariş_tablosu migrasyonu neden yapıldı?"
        raw_text = (
            "def __init__(self):\n"
            "    self._private_var = 'aktif'\n"
            "Veritabanı: sipariş_tablosu güncellendi. Türkçe karakterler: çığöşü."
        )
        contextfold.cmd_fold(step_id, summary, raw_content=raw_text)

        # Verify identifiers extracted correctly
        node_path = contextfold.cold_node_path(step_id)
        with open(node_path, "r", encoding="utf-8") as f:
            node_data = json.load(f)
        self.assertIn("__init__", node_data["identifiers"], "D-3a regression: __init__ was not extracted")
        self.assertIn("_private_var", node_data["identifiers"], "D-3a regression: _private_var was not extracted")
        self.assertIn("sipariş_tablosu", node_data["identifiers"], "N-3: Unicode 'sipariş_tablosu' missing")
        self.assertIn("veritabani", node_data["identifiers"], "D-4: dotless-i folded 'veritabani' missing")
        self.assertIn("istanbul", node_data["identifiers"], "D-4: 'istanbul' should be in identifiers")
        self.assertNotIn("i̇stanbul", node_data["identifiers"], "D-4 / V-2: U+0307 combining dot above must not remain in 'istanbul'")

        # V-6: Turkish stopwords elision verification
        self.assertNotIn("icin", node_data["identifiers"], "V-6: Turkish stopword 'için' was not elided")
        self.assertNotIn("neden", node_data["identifiers"], "V-6: Turkish stopword 'neden' was not elided")

        session = MockHarnessContext(initial_tokens=8100)

        # V-2 Path A: Pure single-token 'istanbul' NFC query (isolates U+0307 and Turkish I/i folding)
        matches_a = session.dispatch_turn("istanbul sorgusu")
        self.assertTrue(len(matches_a) > 0, "V-2 Path A: MMU failed to match isolated 'istanbul' NFC query")
        self.assertEqual(matches_a[0][0], 150, "V-2 Path A: Step 150 should be hydrated")
        self.assertIn(150, session.hydrated_nodes)
        self.assertGreater(session.active_prompt_tokens, 8100)
        session.end_turn()
        self.assertEqual(session.active_prompt_tokens, 8100, "V-2 Path A: Prompt tokens did not return to baseline after eviction")

        # V-2 Path B: Pure single-token NFD 'sipariş_tablosu' query (isolates NFD decomposition & non-ASCII extraction)
        import unicodedata
        nfd_prompt = unicodedata.normalize("NFD", "sipariş_tablosu")
        matches_b = session.dispatch_turn(nfd_prompt)
        self.assertTrue(len(matches_b) > 0, "V-2 Path B: MMU failed to match isolated NFD 'sipariş_tablosu' query")
        self.assertEqual(matches_b[0][0], 150, "V-2 Path B: Step 150 should be hydrated")
        self.assertIn(150, session.hydrated_nodes)
        self.assertGreater(session.active_prompt_tokens, 8100)
        session.end_turn()
        self.assertEqual(session.active_prompt_tokens, 8100, "V-2 Path B: Prompt tokens did not return to baseline after eviction")


    def test_conf_13_multi_intent(self):
        """
        CONF-13 / Invariant 4: Multi-Intent Software MMU Passive Recall (RGSC).
        Verifies:
          1. README §6 showcase query hydrates exactly [342, 137, 447] under default budget.
          2. Superseded steps 91 and 37 are NOT hydrated.
          3. Unrelated / low-coverage step 499 is NOT hydrated (T-1).
          4. S-1 / R-4: Tie-breaking selects chronological newest step on equal residual gain.
        """
        for step_id, summary, raw in contextfold.DEMO_TURNS:
            contextfold.cmd_fold(step_id, summary, raw_content=raw)

        session = MockHarnessContext(initial_tokens=8000)
        showcase_prompt = "What port are we using for PostgreSQL, and which migration applied the billing table?"
        matches = session.dispatch_turn(showcase_prompt)
        hydrated_ids = [m[0] for m in matches]

        self.assertEqual(hydrated_ids, [342, 137, 447], "CONF-13 Multi-intent failed to select [342, 137, 447]")
        self.assertNotIn(91, hydrated_ids, "CONF-13: Superseded step 91 was erroneously hydrated")
        self.assertNotIn(37, hydrated_ids, "CONF-13: Historical step 37 was erroneously hydrated")
        self.assertNotIn(499, hydrated_ids, "CONF-13: Low-coverage step 499 was erroneously hydrated")
        self.assertGreater(session.active_prompt_tokens, 8000)
        session.end_turn()
        self.assertEqual(session.active_prompt_tokens, 8000)

        # R-4 Tie-breaking test: [50, 10, 900]
        contextfold.cmd_fold(50, "alpha beta step", "alpha beta")
        contextfold.cmd_fold(10, "beta gamma old step", "beta gamma")
        contextfold.cmd_fold(900, "gamma newest step", "gamma")
        r4_matches = contextfold.cmd_scan("alpha beta gamma")
        r4_ids = [m[0] for m in r4_matches]
        self.assertEqual(r4_ids, [50, 900], f"R-4 Tie-breaker failed: expected [50, 900], got {r4_ids}")

    def test_conf_06_turn_budget(self):
        """
        CONF-06: Turn-level Rehydration Budget Cap and S-1 Intent Ownership.
        Verifies:
          1. Narrow budget drops oversized candidates without selecting stale/superseded steps.
          2. Under budget=25, Step 91 is NEVER selected in place of Step 137 (S-1 Intent Ownership).
          3. Under budget=30, exactly [342, 137] are selected.
          4. Independent small intents fit in remaining budget even when large intent is dropped (T-1).
        """
        for step_id, summary, raw in contextfold.DEMO_TURNS:
            contextfold.cmd_fold(step_id, summary, raw_content=raw)

        # S-1 test: 25 token budget -> [342]
        policy = contextfold.load_policy()
        policy["max_rehydration_tokens_per_turn"] = 25
        contextfold.atomic_write_json(contextfold.POLICY_FILE, policy)

        res_25 = contextfold.cmd_scan("What port are we using for PostgreSQL, and which migration applied the billing table?")
        ids_25 = [s[0] for s in res_25]
        self.assertEqual(ids_25, [342], "S-1: Under 25 token budget, expected only first candidate [342]")
        self.assertNotIn(91, ids_25, "S-1 VIOLATION: Step 91 (outdated port) was hydrated in place of Step 137!")

        # 30 token budget -> [342, 137]
        policy["max_rehydration_tokens_per_turn"] = 30
        contextfold.atomic_write_json(contextfold.POLICY_FILE, policy)

        res_30 = contextfold.cmd_scan("What port are we using for PostgreSQL, and which migration applied the billing table?")
        ids_30 = [s[0] for s in res_30]
        self.assertEqual(ids_30, [342, 137], "Under 30 token budget, expected [342, 137]")

        # Independent small intent test (T-1): budget=60, large kafka (cost ~75) dropped, small redis (cost ~7) + postgres (cost 16) selected -> [710, 137]
        policy["max_rehydration_tokens_per_turn"] = 60
        contextfold.atomic_write_json(contextfold.POLICY_FILE, policy)
        contextfold.cmd_fold(700, "Kafka broker cluster setup", "kafka broker cluster message log partitions " * 15)
        contextfold.cmd_fold(710, "Redis ttl config", "redis ttl cache configuration")
        small_res = contextfold.cmd_scan("redis ttl kafka broker port postgresql")
        small_ids = [s[0] for s in small_res]
        self.assertEqual(small_ids, [710, 137], f"Expected [710, 137], got {small_ids}")

    def test_conf_12_path_containment(self):
        """
        CONF-12: Path Containment Security Verification (R-2 & S-2).
        Verifies:
          1. Traversal path ('../evil.json') is blocked under both integrity=True and integrity=False.
          2. Prefix sibling ('.contextfold/cold_nodes_evil/...') is blocked.
          3. Absolute path violations are blocked.
          4. cmd_validate captures PathContainmentError per node and continues auditing remaining nodes (T-1).
        """
        contextfold.cmd_fold(100, "Legit node 100", "content 100")
        contextfold.cmd_fold(200, "Legit node 200", "content 200")
        index = contextfold.load_index()

        # Malicious traversal injection
        index["nodes"]["100"]["file_path"] = "../evil.json"
        contextfold.atomic_write_json(contextfold.INDEX_FILE, index)

        # 1. Unconditional blocking under integrity_verification=False and True
        for integrity in [True, False]:
            policy = contextfold.load_policy()
            policy["integrity_verification"] = integrity
            contextfold.atomic_write_json(contextfold.POLICY_FILE, policy)

            with self.assertRaises(contextfold.PathContainmentError, msg=f"Failed to raise PathContainmentError with integrity={integrity}"):
                contextfold.cmd_hydrate(100)

        # 2. Prefix sibling attack
        index["nodes"]["100"]["file_path"] = ".contextfold/cold_nodes_evil/step_000100.json"
        contextfold.atomic_write_json(contextfold.INDEX_FILE, index)
        with self.assertRaises(contextfold.PathContainmentError):
            contextfold.cmd_hydrate(100)

        # 3. Absolute path attack
        index["nodes"]["100"]["file_path"] = "/etc/passwd" if os.name != "nt" else "C:/Windows/secret.json"
        contextfold.atomic_write_json(contextfold.INDEX_FILE, index)
        with self.assertRaises(contextfold.PathContainmentError):
            contextfold.cmd_hydrate(100)

        # 4. S-2 / T-1: cmd_validate error capture and full audit continuation
        import io, contextlib
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            val_result = contextfold.cmd_validate(exit_on_error=False)
        val_output = stdout_buf.getvalue()

        self.assertFalse(val_result, "cmd_validate should return False when path containment violation exists")
        self.assertIn("Path Containment", val_output, "cmd_validate output missing 'Path Containment' error")
        self.assertIn("Step  200", val_output, "cmd_validate aborted early; Step 200 was not audited")


if __name__ == "__main__":
    unittest.main()

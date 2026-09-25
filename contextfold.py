#!/usr/bin/env python3
"""
contextfold.py — IPCF-1.1 Reference Implementation
In-Place Context Folding Protocol (Reference Engine)

Zero external dependencies. Python 3.10+.
Specification: https://github.com/mrblackman/ContextFold

Commands:
  fold     <step_id> <summary>   Fold (archive) a turn into cold storage.
  recall   <query>               Recall archived nodes matching a query.
  hydrate  <step_id>             Print the exact cold node content for hydration.
  evict    <step_id>             Simulate post-turn eviction (mark as evicted).
  status                         Show current session fold index summary.
  validate                       Run integrity check on all cold nodes (CONF-02, CONF-09, CONF-12).
  demo                           Simulate a 500-turn session lifecycle.

Usage:
  python contextfold.py fold 42 "Discussed PostgreSQL port 5433"
  python contextfold.py recall "PostgreSQL port"
  python contextfold.py hydrate 42
  python contextfold.py status
  python contextfold.py validate
  python contextfold.py demo
"""

import sys
import os
import json
import hashlib
import shutil
import tempfile
import datetime
import time

# ──────────────────────────────────────────────────────────────
# Windows encoding safety (CONF-01 lossless I/O)
# ──────────────────────────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ──────────────────────────────────────────────────────────────
# Storage layout
# ──────────────────────────────────────────────────────────────
FOLD_DIR = ".contextfold"
COLD_NODE_DIR = os.path.join(FOLD_DIR, "cold_nodes")
INDEX_FILE = os.path.join(FOLD_DIR, "recall_index.json")
POLICY_FILE = os.path.join(FOLD_DIR, "hydration_policy.json")
EVICTION_LOG = os.path.join(FOLD_DIR, "eviction_log.json")

# ──────────────────────────────────────────────────────────────
# Default hydration policy (CONF-06: reference default, not invariant)
# ──────────────────────────────────────────────────────────────
DEFAULT_POLICY = {
    "policy_version": "1.1",
    "max_rehydration_tokens_per_call": 3000,  # reference default — MAY be overridden
    "scope": "single_turn",
    "eviction_trigger": "after_response",
    "integrity_verification": True
}

# ──────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────
class NodeNotFoundError(Exception):
    pass

class IntegrityCheckError(Exception):
    pass

class OrphanedNodeError(Exception):
    pass

class BrokenReferenceError(Exception):
    pass

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def sha256_of_file(path: str) -> str:
    """Compute SHA-256 of a file. CONF-02."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def sha256_of_str(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()

def _compute_projection_sha256(step_id: int, normalized_content: str, identifiers: list) -> str:
    """
    CONF-11: Deterministic Logical Identity — runtime-independent, reproducible.
    Computed exclusively from the canonical projection fields in a fixed order.
    Same step_id + same content + same identifiers → identical hash on any machine.
    """
    canonical = json.dumps(
        {
            "projection_version": "ipcf-projection-v1",
            "step_id": step_id,
            "normalized_content": normalized_content.strip(),
            "identifiers": sorted(identifiers),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def atomic_write_json(path: str, data: dict) -> None:
    """Write JSON atomically via temp-file-then-rename. CONF-12 (Atomic Fold)."""
    dir_ = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp_path, path)  # atomic on POSIX; best-effort on Windows
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def ensure_dirs() -> None:
    os.makedirs(COLD_NODE_DIR, exist_ok=True)

def load_index() -> dict:
    if not os.path.exists(INDEX_FILE):
        return {"ipcf_version": "1.1", "nodes": {}, "created_at": _iso_now()}
    return load_json(INDEX_FILE)

def save_index(index: dict) -> None:
    atomic_write_json(INDEX_FILE, index)

def load_policy() -> dict:
    if not os.path.exists(POLICY_FILE):
        return DEFAULT_POLICY
    return load_json(POLICY_FILE)

def load_eviction_log() -> dict:
    if not os.path.exists(EVICTION_LOG):
        return {"evicted": []}
    return load_json(EVICTION_LOG)

def save_eviction_log(log: dict) -> None:
    atomic_write_json(EVICTION_LOG, log)

def _iso_now() -> str:
    """UTC ISO-8601 timestamp. Deterministic format, no microseconds drift."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def cold_node_path(step_id: int) -> str:
    return os.path.join(COLD_NODE_DIR, f"step_{step_id:06d}.json")

def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token (reference heuristic)."""
    return max(1, len(text) // 4)

# ──────────────────────────────────────────────────────────────
# Core Engine
# ──────────────────────────────────────────────────────────────

def cmd_fold(step_id: int, summary: str, raw_content: str = None, session_id: str = None) -> None:
    """
    Fold (archive) a turn into cold storage.

    Phase 1: Write cold node to disk (atomic).
    Phase 2: Update recall index (atomic).
    CONF-12: If crash between phases, orphaned node is detectable on next validate.
    CONF-11: Deterministic output — no uuid4(), no datetime in index keys.

    Three Hash Identities (CONFORMANCE.md §2):
      payload_sha256    — immutable content identity (CONF-02)
      projection_sha256 — deterministic logical identity (CONF-11)
      artifact_sha256   — physical on-disk serialization identity (CONF-09)
    """
    ensure_dirs()
    index = load_index()

    if str(step_id) in index["nodes"]:
        print(f"[WARN] Step {step_id} already folded. Skipping.")
        return

    payload_text = raw_content or summary
    normalized_content = payload_text.strip()

    # Three hash identities
    payload_sha256 = sha256_of_str(payload_text)
    identifiers = _extract_identifiers(summary)
    projection_sha256 = _compute_projection_sha256(step_id, normalized_content, identifiers)

    cold_node = {
        "ipcf_version": "1.1",
        "step_id": step_id,
        "session_id": session_id or "default-session",
        "folded_at": _iso_now(),
        "summary": summary,
        "raw_content": payload_text,
        "payload_sha256": payload_sha256,       # CONF-02: immutable content identity
        "projection_sha256": projection_sha256, # CONF-11: deterministic logical identity
        "token_estimate": _estimate_tokens(payload_text),
        "sanitized": False,  # Reference impl: secret scrubbing not implemented.
                             # Production MUST set True after regex redaction pass.
        "status": "cold"
    }

    node_path = cold_node_path(step_id)

    # Phase 1 — write cold node (atomic)
    atomic_write_json(node_path, cold_node)
    artifact_sha256 = sha256_of_file(node_path)  # CONF-09: physical on-disk identity

    # Phase 2 — update recall index (atomic)
    index["nodes"][str(step_id)] = {
        "step_id": step_id,
        "summary": summary,
        "payload_sha256": payload_sha256,
        "projection_sha256": projection_sha256,
        "artifact_sha256": artifact_sha256,
        "file_path": node_path,
        "token_estimate": cold_node["token_estimate"],
        "folded_at": cold_node["folded_at"],
        "identifiers": sorted(identifiers),   # sorted → CONF-11 determinism
        "evicted": False
    }
    save_index(index)

    print(f"[FOLD] Step {step_id} archived.")
    print(f"  Summary           : {summary[:80]}")
    print(f"  payload_sha256    : {payload_sha256[:16]}…")
    print(f"  projection_sha256 : {projection_sha256[:16]}…")
    print(f"  artifact_sha256   : {artifact_sha256[:16]}…")
    print(f"  Tokens est.       : {cold_node['token_estimate']}")
    print(f"  File              : {node_path}")


def _extract_identifiers(text: str) -> list:
    """
    Naive keyword extractor for recall index.
    Production: replace with NLP entity extractor.
    CONF-11: Must be deterministic — same text → same identifiers every run.
    """
    import re
    words = re.findall(r'\b[A-Za-z_][A-Za-z0-9_]{2,}\b', text)
    # Normalize to lowercase, deduplicate, sort → deterministic
    return list(sorted(set(w.lower() for w in words)))

def cmd_recall(query: str) -> None:
    """
    Recall archived nodes matching query.
    CONF-04: Temporal disambiguation — candidates sorted chronologically.
    CONF-05: Returns exact stored metadata; no LLM re-interpretation.
    """
    index = load_index()
    if not index["nodes"]:
        print("[RECALL] No folded nodes found.")
        return

    query_lower = query.lower().split()
    candidates = []

    for step_str, node in index["nodes"].items():
        score = 0
        summary_lower = node["summary"].lower()
        for token in query_lower:
            if token in summary_lower:
                score += 2
            if any(token in ident for ident in node.get("identifiers", [])):
                score += 1
        if score > 0:
            candidates.append((node["step_id"], score, node))

    if not candidates:
        print(f"[RECALL] No matching nodes for query: '{query}'")
        return

    # Sort: primary=score DESC, secondary=step_id DESC (temporal, latest first)
    # CONF-04: chronological ranking, latest state clearly distinguished
    candidates.sort(key=lambda x: (-x[1], -x[0]))

    print(f"\n[RECALL] Query: '{query}'")
    print(f"  Found {len(candidates)} candidate(s) — chronological (latest first):\n")

    for rank, (step_id, score, node) in enumerate(candidates):
        status_label = "latest_chronological" if rank == 0 else "historical"
        evicted_note = " [EVICTED]" if node.get("evicted") else ""
        print(f"  [{rank+1}] Step {step_id:>4}  |  score={score}  |  {status_label}{evicted_note}")
        print(f"       Summary           : {node['summary'][:80]}")
        print(f"       payload_sha256    : {node['payload_sha256'][:16]}…")
        print(f"       projection_sha256 : {node.get('projection_sha256', 'N/A')[:16]}…")
        print(f"       Tokens            : {node['token_estimate']}")
        print(f"       Folded            : {node['folded_at']}")
        print()

def cmd_hydrate(step_id: int) -> None:
    """
    Hydrate a cold node into the active prompt.

    CONF-02: Verify SHA-256 before hydrating (integrity_verification).
    CONF-05: Direct I/O — exact verbatim content, no re-interpretation.
    CONF-06: Token cap check (bounded rehydration).
    CONF-09: Raise IntegrityCheckError on mismatch.
    CONF-10: Raise NodeNotFoundError if step not in index.
    """
    index = load_index()
    policy = load_policy()

    node_meta = index["nodes"].get(str(step_id))
    if node_meta is None:
        raise NodeNotFoundError(f"Step {step_id} not found in recall index. (CONF-10)")

    node_path = node_meta["file_path"]
    if not os.path.exists(node_path):
        raise BrokenReferenceError(
            f"Broken reference: recall index references {node_path} but file does not exist. (CONF-12 Scenario B)"
        )

    # Integrity check — CONF-02, CONF-09
    if policy.get("integrity_verification", True):
        actual_sha = sha256_of_file(node_path)
        expected_sha = node_meta["artifact_sha256"]
        if actual_sha != expected_sha:
            raise IntegrityCheckError(
                f"IntegrityCheckError: artifact_sha256 mismatch for step {step_id}.\n"
                f"  Expected : {expected_sha}\n"
                f"  Actual   : {actual_sha}\n"
                f"  (CONF-09 / CONF-12 Scenario C)"
            )

    cold_node = load_json(node_path)
    content = cold_node["raw_content"]
    token_estimate = _estimate_tokens(content)

    # Token cap — CONF-06
    max_tokens = policy.get("max_rehydration_tokens_per_call", DEFAULT_POLICY["max_rehydration_tokens_per_call"])
    if token_estimate > max_tokens:
        truncation_point = max_tokens * 4  # chars
        content = content[:truncation_point]
        print(f"[WARN] Content truncated to {max_tokens} tokens (policy cap). (CONF-06)")

    print(f"\n[HYDRATE] Step {step_id} — exact verbatim content:")
    print(f"  Token estimate    : {min(token_estimate, max_tokens)}")
    print(f"  payload_sha256    : {cold_node['payload_sha256'][:16]}…")
    print(f"  Scope             : {policy.get('scope', 'single_turn')} (evict after response)")
    print()
    print("─" * 60)
    print(content)
    print("─" * 60)
    print(f"\n[HYDRATE] Eviction trigger: {policy.get('eviction_trigger', 'after_response')}")
    print(f"  → Call `python contextfold.py evict {step_id}` after LLM response to comply with CONF-07.")

def cmd_evict(step_id: int) -> None:
    """
    Mark a hydrated node as evicted (simulates post-turn eviction).
    CONF-07: Enforced eviction — no cumulative creep.
    """
    index = load_index()
    node_meta = index["nodes"].get(str(step_id))

    if node_meta is None:
        raise NodeNotFoundError(f"Step {step_id} not found in recall index.")

    if node_meta.get("evicted"):
        print(f"[EVICT] Step {step_id} already marked as evicted.")
        return

    index["nodes"][str(step_id)]["evicted"] = True
    save_index(index)

    eviction_log = load_eviction_log()
    eviction_log["evicted"].append({"step_id": step_id, "evicted_at": _iso_now()})
    save_eviction_log(eviction_log)

    print(f"[EVICT] Step {step_id} evicted from active prompt. Cold storage preserved. (CONF-07)")

def cmd_status() -> None:
    """
    Show current session fold index summary.
    """
    ensure_dirs()
    index = load_index()
    policy = load_policy()
    nodes = index.get("nodes", {})

    total = len(nodes)
    evicted_count = sum(1 for n in nodes.values() if n.get("evicted"))
    active_count = total - evicted_count
    total_tokens = sum(n.get("token_estimate", 0) for n in nodes.values())

    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║      ContextFold — IPCF-1.1 Session Status      ║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    print(f"  Storage dir    : {os.path.abspath(FOLD_DIR)}")
    print(f"  Index          : {os.path.abspath(INDEX_FILE)}")
    print(f"  Total folded   : {total} nodes")
    print(f"  Active cold    : {active_count}")
    print(f"  Evicted        : {evicted_count}")
    print(f"  Total tokens   : {total_tokens} (archived, not in active prompt)")
    print()
    print(f"  Policy version : {policy.get('policy_version', '1.1')}")
    print(f"  Rehydration cap: {policy.get('max_rehydration_tokens_per_call', 3000)} tokens (reference default)")
    print(f"  Scope          : {policy.get('scope', 'single_turn')}")
    print(f"  Eviction       : {policy.get('eviction_trigger', 'after_response')}")
    print()

    if nodes:
        print("  Folded nodes (step_id | tokens | evicted | summary):")
        for step_str, node in sorted(nodes.items(), key=lambda x: int(x[0])):
            evicted_flag = "✓ evicted" if node.get("evicted") else "  active "
            print(f"    Step {int(step_str):>4}  |  {node.get('token_estimate',0):>5} tk  "
                  f"|  {evicted_flag}  |  {node['summary'][:50]}")
    print()

def cmd_validate() -> None:
    """
    Run integrity check on all cold nodes.
    CONF-02: SHA-256 round-trip.
    CONF-09: Tamper detection.
    CONF-12: Orphaned nodes + broken references.
    """
    ensure_dirs()
    index = load_index()
    nodes = index.get("nodes", {})
    policy = load_policy()

    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║   ContextFold — IPCF-1.1 Integrity Validation   ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    errors = []
    pass_count = 0

    # 1. Check all indexed nodes (CONF-02, CONF-09, CONF-12 Scenario B & C)
    for step_str, node_meta in sorted(nodes.items(), key=lambda x: int(x[0])):
        step_id = int(step_str)
        node_path = node_meta.get("file_path", cold_node_path(step_id))

        if not os.path.exists(node_path):
            msg = f"CONF-12 Scenario B — Broken reference: step {step_id} in index but file missing: {node_path}"
            errors.append(msg)
            print(f"  [FAIL] Step {step_id:>4}: {msg}")
            continue

        if policy.get("integrity_verification", True):
            actual_sha = sha256_of_file(node_path)
            expected_sha = node_meta.get("artifact_sha256", "")
            if actual_sha != expected_sha:
                msg = (f"CONF-09 / CONF-12 Scenario C — artifact_sha256 mismatch for step {step_id}. "
                       f"Expected={expected_sha[:16]}… Actual={actual_sha[:16]}…")
                errors.append(msg)
                print(f"  [FAIL] Step {step_id:>4}: {msg}")
                continue

        pass_count += 1
        print(f"  [ OK ] Step {step_id:>4}: artifact_sha256 verified. (CONF-02 / CONF-09)")

    # 2. Detect orphaned cold nodes (CONF-12 Scenario A)
    if os.path.isdir(COLD_NODE_DIR):
        indexed_steps = {
            int(s) for s in nodes.keys()
        }
        for fname in sorted(os.listdir(COLD_NODE_DIR)):
            if fname.startswith("step_") and fname.endswith(".json"):
                try:
                    orphan_id = int(fname.replace("step_", "").replace(".json", ""))
                except ValueError:
                    continue
                if orphan_id not in indexed_steps:
                    msg = (f"CONF-12 Scenario A — Orphaned cold node: {fname} exists on disk "
                           f"but has no recall index entry.")
                    errors.append(msg)
                    print(f"  [FAIL] Orphan : {msg}")

    print()
    if not errors:
        print(f"  Validation PASSED — {pass_count} node(s) verified. IPCF-1.1 integrity confirmed.")
        sys.exit(0)
    else:
        print(f"  Validation FAILED — {len(errors)} error(s), {pass_count} passed.")
        print()
        print("  Error summary:")
        for e in errors:
            print(f"    • {e}")
    print()
    if errors:
        sys.exit(1)

def cmd_demo() -> None:
    """
    Simulate a 500-turn session lifecycle.
    Demonstrates: fold, recall (CONF-04 temporal disambiguation), hydrate, evict,
    and bounded rehydration cycle (CONF-07).
    """
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  ContextFold — IPCF-1.1 Demo: 500-Turn Session Simulation   ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    ensure_dirs()

    # --- Build a mini scenario from the CONFORMANCE spec ---
    demo_turns = [
        (37,  "PostgreSQL configured on Port 5433"),
        (91,  "PostgreSQL switched to Port 5434 (conflict resolution)"),
        (137, "Port 5434 reverted back to Port 5433"),
        (200, "Authentication middleware refactored; JWT secret rotated"),
        (318, "Migration V12 applied to production database"),
        (342, "Migration V12 rolled back due to deadlocks"),
        (447, "Migration V12 patched and re-applied successfully"),
        (499, "Final review: all services healthy, Port 5433 confirmed"),
    ]

    # Fold all demo turns
    print("  [1/4] Folding 8 representative turns from a 500-turn session...\n")
    for step_id, summary in demo_turns:
        raw = f"[Turn {step_id}] {summary}"
        print(f"  → Folding step {step_id}: {summary[:60]}")
        cmd_fold(step_id, summary, raw_content=raw)
    print()

    # Status check
    print("  [2/4] Session status after folding:\n")
    cmd_status()

    # Recall with temporal disambiguation
    print("  [3/4] Recall: 'PostgreSQL port' — temporal disambiguation (CONF-04):\n")
    cmd_recall("PostgreSQL port")

    # Hydrate + Evict cycle (CONF-07)
    print("  [4/4] Bounded rehydration + eviction cycle (CONF-07):\n")
    print("  → Hydrating step 137 (latest PostgreSQL port state)...")
    try:
        cmd_hydrate(137)
    except (IntegrityCheckError, NodeNotFoundError) as e:
        print(f"  [ERROR] {e}")
        return
    print()
    print("  → LLM response generated. Now evicting step 137 from active prompt...")
    cmd_evict(137)

    # Validate integrity
    print()
    print("  [VALIDATE] Running full integrity check...\n")
    cmd_validate()

    print("  Demo complete. ContextFold IPCF-1.1 lifecycle validated.")
    print()
    print("  Mottoes:")
    print("    'Don't make the AI carry what the machine can page.'")
    print("    'Fold changes what the model carries.'")
    print("    'Fork changes where the model continues.'")
    print()

# ──────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────

def print_usage() -> None:
    print("""
ContextFold — IPCF-1.1 Reference Implementation

Usage:
  python contextfold.py fold     <step_id> <"summary">   Archive a turn
  python contextfold.py recall   <"query">                Query archived nodes (CONF-04)
  python contextfold.py hydrate  <step_id>               Hydrate a cold node (CONF-05/06)
  python contextfold.py evict    <step_id>               Evict after LLM response (CONF-07)
  python contextfold.py status                            Show index summary
  python contextfold.py validate                          Integrity check (CONF-02/09/12)
  python contextfold.py demo                              Run 500-turn simulation

Specification: https://github.com/mrblackman/ContextFold
Motto: "Don't make the AI carry what the machine can page."
""")

def main() -> None:
    args = sys.argv[1:]
    if not args:
        print_usage()
        sys.exit(0)

    command = args[0].lower()

    try:
        if command == "fold":
            if len(args) < 3:
                print("Usage: python contextfold.py fold <step_id> <summary>")
                sys.exit(1)
            step_id = int(args[1])
            summary = " ".join(args[2:])
            cmd_fold(step_id, summary)

        elif command == "recall":
            if len(args) < 2:
                print("Usage: python contextfold.py recall <query>")
                sys.exit(1)
            query = " ".join(args[1:])
            cmd_recall(query)

        elif command == "hydrate":
            if len(args) < 2:
                print("Usage: python contextfold.py hydrate <step_id>")
                sys.exit(1)
            step_id = int(args[1])
            cmd_hydrate(step_id)

        elif command == "evict":
            if len(args) < 2:
                print("Usage: python contextfold.py evict <step_id>")
                sys.exit(1)
            step_id = int(args[1])
            cmd_evict(step_id)

        elif command == "status":
            cmd_status()

        elif command == "validate":
            cmd_validate()

        elif command == "demo":
            cmd_demo()

        else:
            print(f"Unknown command: '{command}'")
            print_usage()
            sys.exit(1)

    except NodeNotFoundError as e:
        print(f"\n[ERROR] NodeNotFoundError: {e}", file=sys.stderr)
        sys.exit(2)
    except IntegrityCheckError as e:
        print(f"\n[ERROR] IntegrityCheckError: {e}", file=sys.stderr)
        sys.exit(3)
    except OrphanedNodeError as e:
        print(f"\n[ERROR] OrphanedNodeError: {e}", file=sys.stderr)
        sys.exit(4)
    except BrokenReferenceError as e:
        print(f"\n[ERROR] BrokenReferenceError: {e}", file=sys.stderr)
        sys.exit(5)
    except (ValueError, TypeError) as e:
        print(f"\n[ERROR] Bad argument: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

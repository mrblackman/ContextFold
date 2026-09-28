#!/usr/bin/env python3
"""
contextfold.py — IPCF-1.1 Reference Implementation
In-Place Context Folding Protocol (Reference Engine)

Zero external dependencies. Python 3.10+.
Specification: https://github.com/mrblackman/ContextFold

Commands:
  fold     <step_id> <summary>   Fold (archive) a turn into cold storage.
  scan     <prompt>              Passively scan prompt against recall index (CONF-13 Software MMU).
  recall   <query>               Recall archived nodes matching a query (pull-based).
  hydrate  <step_id>             Print the exact cold node content for hydration (CONF-05/06).
  evict    <step_id>             Simulate post-turn eviction (CONF-07).
  status                         Show current session fold index summary.
  validate                       Run integrity check on all cold nodes (CONF-02, CONF-09, CONF-12).
  demo                           Simulate a 500-turn session lifecycle.

Usage:
  python contextfold.py fold 42 "Discussed PostgreSQL port 5433" [--file path] [--files f1,f2] [--commands c1,c2] [--errors e1,e2]
  python contextfold.py scan "Which port did we assign to Postgres?"
  python contextfold.py recall "PostgreSQL port"
  python contextfold.py hydrate 42
  python contextfold.py status
  python contextfold.py validate
  python contextfold.py demo
"""

import sys
import os
from pathlib import Path
import json
import hashlib
import tempfile
import datetime
import re
import unicodedata

# ──────────────────────────────────────────────────────────────
# Windows encoding safety (CONF-01 lossless I/O)
# ──────────────────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────
# Storage layout & Protocol Versions (D-3)
# ──────────────────────────────────────────────────────────────
PROJECTION_VERSION = "ipcf-projection-v2"

FOLD_DIR = ".contextfold"
COLD_NODE_DIR = os.path.join(FOLD_DIR, "cold_nodes")
INDEX_FILE = os.path.join(FOLD_DIR, "recall_index.json")
POLICY_FILE = os.path.join(FOLD_DIR, "hydration_policy.json")
EVICTION_LOG = os.path.join(FOLD_DIR, "eviction_log.json")

# ──────────────────────────────────────────────────────────────
# 500-Turn Verification Scenario Fixture (D-2, D-5)
# ──────────────────────────────────────────────────────────────
DEMO_TURNS = [
    (37,  "PostgreSQL configured on Port 5433", "$ docker compose up -d postgres\nPostgreSQL running on port 5433"),
    (91,  "PostgreSQL switched to Port 5434 (conflict resolution)", "$ psql -p 5434 -U admin\nConflict resolved on port 5434"),
    (137, "PostgreSQL port 5434 reverted back to Port 5433", "$ sed -i 's/5434/5433/' config.env\nPostgreSQL Port 5433 restored"),
    (200, "Authentication middleware refactored; JWT secret rotated", "JWT secret updated in auth/middleware.py"),
    (318, "Migration V12 applied to production database", "$ python manage.py migrate V12__TenantBilling.sql\nMigration V12 applied"),
    (342, "Migration V12 rolled back due to deadlocks", "Deadlock detected in billing table; rolled back V12"),
    (447, "Migration V12 patched and re-applied successfully", "Patched V12__TenantBilling.sql with row-level locks; migration complete"),
    (499, "Final review: all services healthy, Port 5433 confirmed", "All health checks passing on port 5433"),
]

# ──────────────────────────────────────────────────────────────
# Recall Candidate Model (D-1, D-6)
# ──────────────────────────────────────────────────────────────
class RecallCandidate(dict):
    """
    Candidate dict returned by cmd_recall.
    Supports standard dict keys ('step_id', 'status', 'coverage', 'score', 'node')
    and legacy tuple index lookup [0: step_id, 1: score, 2: node] as a dict adapter (V-3).
    Note: Does not support sequence unpacking (e.g. step_id, score, node = candidate).
    """
    def __getitem__(self, item):
        if isinstance(item, int):
            if item == 0:
                return self["step_id"]
            elif item == 1:
                return self["score"]
            elif item == 2:
                node = self["node"]
                node["status"] = self["status"]
                return node
        return super().__getitem__(item)


# ──────────────────────────────────────────────────────────────
# Default hydration policy (CONF-06: reference default, not invariant)
# ──────────────────────────────────────────────────────────────
DEFAULT_POLICY = {
    "policy_version": "1.1",
    "max_rehydration_tokens_per_call": 3000,
    "max_rehydration_tokens_per_turn": 3500,
    "scope": "single_turn",
    "eviction_trigger": "after_response",
    "integrity_verification": True
}

# ──────────────────────────────────────────────────────────────
# Errors (CONF-09, CONF-10, CONF-12)
# ──────────────────────────────────────────────────────────────
class NodeNotFoundError(Exception):
    """Raised when a requested step_id is absent from the index (CONF-10)."""
    pass

class IntegrityCheckError(Exception):
    """Raised when SHA-256 hash verification fails (CONF-09, CONF-12 Scenario C)."""
    pass

class PathContainmentError(IntegrityCheckError):
    """Raised when cold storage file path violates canonical boundary or attempts traversal (CONF-12 Path Containment)."""
    pass

class OrphanedNodeError(Exception):
    """Raised when a cold storage node exists on disk but has no recall index entry (CONF-12 Scenario A)."""
    pass

class BrokenReferenceError(Exception):
    """Raised when recall index references a cold node file that does not exist on disk (CONF-12 Scenario B)."""
    pass

# ──────────────────────────────────────────────────────────────
# Serialization & Canonical Hashing
# ──────────────────────────────────────────────────────────────

def canonical_json_bytes(data: dict) -> bytes:
    """
    Canonical JSON serialization for IPCF-1.1 hashing.
    sort_keys=True, separators=(',', ':'), ensure_ascii=False, UTF-8 encoded.
    CONFORMANCE.md §2.3 normative definition.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_of_str(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()

def sha256_of_file(path: str) -> str:
    """Compute SHA-256 of raw bytes of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def _compute_projection_sha256(
    step_id: int,
    normalized_content: str,
    identifiers: list,
    files: list,
    commands: list,
    errors: list,
    policy_dict: dict
) -> str:
    """
    CONF-11: Deterministic Logical Identity — runtime-independent, reproducible.
    Computed strictly from the 8 canonical projection fields in normative order (CONFORMANCE.md §2.2):
    1. projection_version
    2. step_id
    3. normalized_content
    4. identifiers (sorted)
    5. files (sorted)
    6. commands (sorted)
    7. errors (sorted)
    8. folding_policy_hash (SHA-256 of canonical JSON of active policy)
    """
    policy_canonical_hash = sha256_of_bytes(canonical_json_bytes(policy_dict))
    proj_obj = {
        "projection_version": PROJECTION_VERSION,
        "step_id": step_id,
        "normalized_content": normalized_content.rstrip(),
        "identifiers": sorted(list(set(identifiers))),
        "files": sorted(list(set(files))),
        "commands": sorted(list(set(commands))),
        "errors": sorted(list(set(errors))),
        "folding_policy_hash": policy_canonical_hash,
    }
    return sha256_of_bytes(canonical_json_bytes(proj_obj))

def _resolve_cold_node_path(step_id: int, node_meta: dict) -> str:
    """
    Unconditionally verify and resolve canonical cold node path against path traversal,
    absolute paths, sibling directory prefixes, and symlink escapes (CONF-12 Path Containment).
    Runs unconditionally, independent of policy['integrity_verification'].
    """
    node_path = node_meta.get("file_path", "")
    if not node_path:
        raise PathContainmentError(f"Step {step_id}: Missing file_path in recall_index node metadata.")

    expected_path = cold_node_path(step_id)
    resolved_node = Path(node_path).resolve()
    resolved_expected = Path(expected_path).resolve()
    resolved_cold_dir = Path(COLD_NODE_DIR).resolve()

    if resolved_node != resolved_expected or resolved_node.parent != resolved_cold_dir:
        raise PathContainmentError(
            f"PathContainmentError: Cold node path '{node_path}' violates canonical boundary or does not match expected '{expected_path}' for step {step_id}."
        )
    return str(resolved_expected)

def verify_artifact(path: str, expected_hash: str = None) -> tuple[bool, dict, str]:
    """
    Verify canonical physical on-disk serialization integrity (CONF-09, CONF-12).
    Two-Phase Verification:
      1. Read cold node JSON.
      2. Extract stored artifact_sha256.
      3. Construct canonical representation with artifact_sha256 omitted.
      4. Compute SHA-256 and assert stored_hash == computed_hash (and == expected_hash if provided).
    """
    if not os.path.exists(path):
        raise BrokenReferenceError(f"Cold node file not found: {path} (CONF-12 Scenario B)")

    try:
        data = load_json(path)
    except (json.JSONDecodeError, ValueError) as err:
        raise IntegrityCheckError(f"Corrupted or truncated JSON in cold node file: {path} (CONF-12 Scenario C)") from err

    stored_hash = data.get("artifact_sha256")
    if not stored_hash:
        raise IntegrityCheckError(f"Missing artifact_sha256 in cold node file: {path} (CONF-09)")

    # Phase 1 shape: object without artifact_sha256
    verify_obj = {k: v for k, v in data.items() if k != "artifact_sha256"}
    computed_hash = sha256_of_bytes(canonical_json_bytes(verify_obj))

    if stored_hash != computed_hash:
        raise IntegrityCheckError(
            f"IntegrityCheckError: artifact_sha256 mismatch for {path}.\n"
            f"  Stored in file : {stored_hash}\n"
            f"  Computed       : {computed_hash}\n"
            f"  (CONF-09 / CONF-12 Scenario C)"
        )

    if expected_hash and stored_hash != expected_hash:
        raise IntegrityCheckError(
            f"IntegrityCheckError: artifact_sha256 does not match recall_index for {path}.\n"
            f"  Index expected: {expected_hash}\n"
            f"  File stored   : {stored_hash}\n"
            f"  (CONF-09 / CONF-12 Scenario C)"
        )

    return True, data, computed_hash

def atomic_write_json(path: str, data: dict) -> None:
    """Write JSON atomically via temp-file-then-rename. CONF-12 (Atomic Fold)."""
    dir_ = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp_path, path)
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
        return {
            "ipcf_version": "1.1",
            "projection_version": PROJECTION_VERSION,
            "nodes": {},
            "created_at": _iso_now()
        }
    return load_json(INDEX_FILE)

def save_index(index: dict) -> None:
    atomic_write_json(INDEX_FILE, index)

def load_policy() -> dict:
    if not os.path.exists(POLICY_FILE):
        return dict(DEFAULT_POLICY)
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
# Deterministic Heuristic Extractors (Z-3, Z-4, Y-3, Y-5)
# ──────────────────────────────────────────────────────────────

def _fold_token(w: str) -> str:
    """
    NFC normalize, lowercase, strip combining dot above (U+0307), and fold Turkish dotless i (D-4).
    Deterministic token folding for identifiers and recall matching.
    """
    normalized = unicodedata.normalize("NFC", w.lower())
    return normalized.replace("\u0307", "").replace("ı", "i")

ENGLISH_STOPWORDS = {
    "and", "the", "for", "with", "this", "that", "from", "are", "was",
    "were", "will", "can", "has", "had", "have", "what", "which", "how",
    "did", "you", "all", "any", "not", "but",
    # 2-letter common English words to avoid noise (Z-3)
    "we", "is", "it", "on", "at", "to", "of", "in", "do", "be", "or",
    "an", "as", "if", "by", "so", "no", "up", "my"
}

TURKISH_STOPWORDS = {
    "ve", "ile", "için", "olan", "bir", "bu", "şu", "daha", "gibi",
    "kadar", "diye", "veya", "ya", "ama", "fakat", "lakin", "çünkü",
    "böyle", "nasıl", "neden", "ne", "hangi", "var", "yok", "şey",
    "mi", "mu", "mü", "mı"
}

STOPWORDS = {
    _fold_token(w) for w in (ENGLISH_STOPWORDS | TURKISH_STOPWORDS)
} | ENGLISH_STOPWORDS | TURKISH_STOPWORDS

def _extract_files(text: str) -> list[str]:
    pattern = r'(?:[\w.-]+[/\\])+[\w.-]+\.[a-zA-Z0-9]+|[\w.-]+\.(?:py|js|ts|json|md|sql|sh|yml|yaml|html|css|rs|go|cs)'
    matches = re.findall(pattern, text)
    return sorted(list(set(m.replace('\\', '/') for m in matches)))

def _extract_commands(text: str) -> list[str]:
    # Y-3: re.MULTILINE with $ or > prompt symbols
    pattern = r'^\s*(?:\$|>)\s*([a-zA-Z0-9_.-]+(?:\s+[^;\n\r]+)?)'
    matches = re.findall(pattern, text, re.MULTILINE)
    return sorted(list(set(m.strip() for m in matches if m.strip())))

def _extract_errors(text: str) -> list[str]:
    pattern = r'\b(?:[A-Z][a-zA-Z0-9]+Error|[A-Z][a-zA-Z0-9]+Exception|FATAL|PANIC|Traceback)\b|HTTP\s+[45]\d{2}|\b[A-Z]{2,}_[A-Z0-9_]+_ERROR\b'
    matches = re.findall(pattern, text)
    return sorted(list(set(matches)))

def _extract_identifiers(text: str) -> list[str]:
    # D-3a: Starts with letter or underscore, 3+ chars: r'\b[^\W\d]\w{2,}\b'
    # Or numbers 3+ digits: r'\b[0-9]{3,}\b'
    # Unicode word support (N-3) and Turkish case-folding (D-4)
    text_nfc = unicodedata.normalize("NFC", text)
    pattern = r'\b[^\W\d]\w{2,}\b|\b[0-9]{3,}\b'
    matches = re.findall(pattern, text_nfc)
    folded = [_fold_token(w) for w in matches]
    return sorted(list(set(w for w in folded if w not in STOPWORDS)))

# ──────────────────────────────────────────────────────────────
# Core Engine
# ──────────────────────────────────────────────────────────────

def cmd_fold(
    step_id: int,
    summary: str,
    raw_content: str = None,
    session_id: str = None,
    files: list = None,
    commands: list = None,
    errors: list = None
) -> dict:
    """
    Fold (archive) a turn into cold storage.

    Two-Phase Write Protocol (CONFORMANCE.md §2.3):
      Phase 1: Compute payload_sha256, 8-field projection_sha256, and canonical artifact_sha256.
      Phase 2: Atomic write cold node with artifact_sha256 included; update recall_index.json.
    """
    ensure_dirs()
    index = load_index()

    if str(step_id) in index["nodes"]:
        print(f"[WARN] Step {step_id} already folded. Skipping.")
        return index["nodes"][str(step_id)]

    payload_text = raw_content or summary
    summary_nfc = unicodedata.normalize("NFC", summary)
    payload_nfc = unicodedata.normalize("NFC", payload_text)
    normalized_content = payload_nfc.rstrip()

    policy = load_policy()

    # Extract or take explicit fields (W-4b: use NFC normalized text for deterministic extraction)
    combined_text = f"{summary_nfc}\n{payload_nfc}"
    extracted_files = files if files is not None else _extract_files(combined_text)
    extracted_commands = commands if commands is not None else _extract_commands(combined_text)
    extracted_errors = errors if errors is not None else _extract_errors(combined_text)
    extracted_identifiers = _extract_identifiers(combined_text)

    # Three hash identities (CONF-02 preserves raw payload for payload_sha256)
    payload_sha256 = sha256_of_str(payload_text)
    projection_sha256 = _compute_projection_sha256(
        step_id=step_id,
        normalized_content=normalized_content,
        identifiers=extracted_identifiers,
        files=extracted_files,
        commands=extracted_commands,
        errors=extracted_errors,
        policy_dict=policy
    )

    created_iso = _iso_now()

    # Construct Phase 1 cold node object (artifact_sha256 absent)
    cold_node_pre = {
        "ipcf_version": "1.1",
        "projection_version": PROJECTION_VERSION,
        "step_id": step_id,
        "session_id": session_id or "default-session",
        "folded_at": created_iso,
        "summary": summary,
        "raw_content": payload_text,
        "normalized_content": normalized_content,
        "payload_sha256": payload_sha256,
        "projection_sha256": projection_sha256,
        "identifiers": extracted_identifiers,
        "files": extracted_files,
        "commands": extracted_commands,
        "errors": extracted_errors,
        "token_estimate": _estimate_tokens(payload_text),
        "sanitized": False,
        "status": "cold"
    }

    # Phase 1: Compute artifact_sha256 over canonical serialization
    artifact_sha256 = sha256_of_bytes(canonical_json_bytes(cold_node_pre))

    # Phase 2: Insert artifact_sha256 and persist atomically
    cold_node = dict(cold_node_pre)
    cold_node["artifact_sha256"] = artifact_sha256

    node_path = cold_node_path(step_id)
    atomic_write_json(node_path, cold_node)

    # Use POSIX forward slashes for portability
    posix_node_path = node_path.replace("\\", "/")

    # Update recall index
    index["nodes"][str(step_id)] = {
        "step_id": step_id,
        "projection_version": PROJECTION_VERSION,
        "summary": summary,
        "payload_sha256": payload_sha256,
        "projection_sha256": projection_sha256,
        "artifact_sha256": artifact_sha256,
        "file_path": posix_node_path,
        "token_estimate": cold_node["token_estimate"],
        "folded_at": created_iso,
        "identifiers": extracted_identifiers,
        "files": extracted_files,
        "commands": extracted_commands,
        "errors": extracted_errors,
        "evicted": False
    }
    save_index(index)

    print(f"[FOLD] Step {step_id} archived.")
    print(f"  Summary           : {summary[:80]}")
    print(f"  payload_sha256    : {payload_sha256[:16]}…")
    print(f"  projection_sha256 : {projection_sha256[:16]}…")
    print(f"  artifact_sha256   : {artifact_sha256[:16]}…")
    print(f"  Tokens est.       : {cold_node['token_estimate']}")
    print(f"  File              : {posix_node_path}")

    return cold_node

def cmd_recall(query: str) -> list[RecallCandidate]:
    """
    Recall archived nodes matching query.
    CONF-04: Temporal disambiguation via Coverage Tiering + Chronological Ranking (D-1, D-6).
    CONF-05: Returns exact stored metadata; no LLM re-interpretation.
    """
    index = load_index()
    if not index["nodes"]:
        print("[RECALL] No folded nodes found.")
        return []

    # D-4, V-1: NFC normalize and extract normalized tokens via exact term semantics
    query_nfc = unicodedata.normalize("NFC", query)
    query_tokens = set(_extract_identifiers(query_nfc))
    all_query_terms = query_tokens

    candidates = []

    for step_str, node in index["nodes"].items():
        step_id = int(step_str)
        summary_tokens = set(_extract_identifiers(unicodedata.normalize("NFC", node.get("summary", ""))))
        indexed_terms = (
            set(node.get("identifiers", [])) |
            set(_fold_token(f) for f in node.get("files", [])) |
            set(_fold_token(e) for e in node.get("errors", []))
        )
        
        # Calculate coverage: distinct query terms matched with exact term semantics (V-1)
        matched_query_terms = set()
        score = 0
        for token in all_query_terms:
            matched = False
            if token in summary_tokens:
                score += 2
                matched = True
            if token in indexed_terms:
                score += 1
                matched = True
            if matched:
                matched_query_terms.add(token)

        coverage = len(matched_query_terms)
        if coverage > 0 or score > 0:
            candidates.append(RecallCandidate({
                "step_id": step_id,
                "score": score,
                "coverage": coverage,
                "status": "pending",
                "node": node
            }))

    if not candidates:
        print(f"[RECALL] No matching nodes for query: '{query}'")
        return []

    # D-1: Coverage Tiering
    max_coverage = max(c["coverage"] for c in candidates)
    primary_tier = [c for c in candidates if c["coverage"] == max_coverage]
    secondary_tier = [c for c in candidates if c["coverage"] < max_coverage]

    # Sort each tier chronologically (latest first: step_id DESC)
    primary_tier.sort(key=lambda x: -x["step_id"])
    secondary_tier.sort(key=lambda x: -x["step_id"])

    # D-6: Normative status labels (CONFORMANCE.md §4.1)
    n_primary = len(primary_tier)
    for i, c in enumerate(primary_tier):
        if n_primary == 1:
            c["status"] = "latest_chronological"
        elif n_primary == 2:
            c["status"] = "latest_chronological" if i == 0 else "historical"
        else: # n >= 3
            if i == 0:
                c["status"] = "latest_chronological"
            elif i == n_primary - 1:
                c["status"] = "historical"
            else:
                c["status"] = "superseded"

    for c in secondary_tier:
        c["status"] = "related"

    ordered_candidates = primary_tier + secondary_tier

    print(f"\n[RECALL] Query: '{query}'")
    print(f"  Found {len(ordered_candidates)} candidate(s) — coverage tiered & chronological (latest first):\n")

    for rank, c in enumerate(ordered_candidates):
        step_id = c["step_id"]
        node = c["node"]
        status_label = c["status"]
        evicted_note = " [EVICTED]" if node.get("evicted") else ""
        print(f"  [{rank+1}] Step {step_id:>4}  |  coverage={c['coverage']}  score={c['score']}  |  {status_label}{evicted_note}")
        print(f"       Summary           : {node['summary'][:80]}")
        print(f"       payload_sha256    : {node['payload_sha256'][:16]}…")
        print(f"       projection_sha256 : {node.get('projection_sha256', 'N/A')[:16]}…")
        print(f"       Tokens            : {node['token_estimate']}")
        print(f"       Folded            : {node['folded_at']}")
        print()

    return ordered_candidates

def cmd_scan(prompt: str) -> list:
    """
    CONF-13 / Invariant 4: Software MMU Passive Recall via Residual Greedy Set Cover (RGSC).
    Extracts identifiers, files, errors from prompt and matches against indexed terms.
    Applies Intent Ownership (S-1) and tie-breaking (R-4) under turn token budget.
    Returns list of (step_id, matched_tokens, node) ordered by selection priority.
    """
    index = load_index()
    policy = load_policy()
    if not index["nodes"]:
        print("[SCAN] Recall index is empty.")
        return []

    prompt_nfc = unicodedata.normalize("NFC", prompt)
    prompt_tokens = set(_extract_identifiers(prompt_nfc))
    prompt_files = set(_fold_token(f) for f in _extract_files(prompt_nfc))
    prompt_errors = set(_fold_token(e) for e in _extract_errors(prompt_nfc))
    all_query_tokens = prompt_tokens | prompt_files | prompt_errors

    matched_candidates = []
    for step_str, node in index["nodes"].items():
        node_terms = (
            set(node.get("identifiers", [])) |
            set(_fold_token(f) for f in node.get("files", [])) |
            set(_fold_token(e) for e in node.get("errors", []))
        )
        intersection = all_query_tokens.intersection(node_terms)
        if intersection:
            matched_candidates.append((int(step_str), intersection, node, node_terms))

    # Initial sort: match richness DESC, step_id DESC (D-1)
    matched_candidates.sort(key=lambda x: (-len(x[1]), -x[0]))

    residual = set(all_query_tokens)
    budget_limit = policy.get("max_rehydration_tokens_per_turn", DEFAULT_POLICY["max_rehydration_tokens_per_turn"])
    max_per_call = policy.get("max_rehydration_tokens_per_call", DEFAULT_POLICY["max_rehydration_tokens_per_call"])
    consumed_tokens = 0
    selected_results = []
    selected_ids = set()
    budget_dropped = []

    # RGSC selection with Intent Ownership (S-1) & Tie-Breaking (R-4)
    while residual:
        best_candidate = None
        best_gain = 0
        best_step_id = -1

        for step_id, _, node, node_terms in matched_candidates:
            if step_id in selected_ids:
                continue
            gain = len(residual & node_terms)
            if gain == 0:
                continue
            if (gain > best_gain) or (gain == best_gain and step_id > best_step_id):
                best_gain = gain
                best_step_id = step_id
                best_candidate = (step_id, sorted(list(residual & node_terms)), node, node_terms)

        if best_candidate is None:
            break

        step_id, matched_residual, node, node_terms = best_candidate
        cost = min(node.get("token_estimate", 0), max_per_call, budget_limit)

        # S-1 Intent Ownership:
        # First candidate is ALWAYS admitted (clipped by per-call/per-turn limit)
        # Subsequent candidates must fit within remaining turn budget
        if len(selected_results) == 0 or (consumed_tokens + cost <= budget_limit):
            selected_results.append((step_id, matched_residual, node))
            selected_ids.add(step_id)
            consumed_tokens += cost
            residual -= node_terms
        else:
            # Exceeds budget: drop terms so superseded/stale steps cannot steal the intent!
            residual -= node_terms
            budget_dropped.append((step_id, matched_residual, cost))

    print(f"\n[SCAN (Software MMU - CONF-13)] Passively intercepting incoming prompt:")
    print(f"  Prompt : '{prompt}'")
    print(f"  Extracted Tokens : {sorted(list(all_query_tokens))}")

    if selected_results:
        print(f"  ⚡ MMU INTERCEPT: {len(matched_candidates)} candidate historical step(s) matched in index:")
        for step_id, common_tokens, node, _ in matched_candidates:
            status_mark = " [SELECTED]" if step_id in selected_ids else ""
            print(f"     • Step {step_id:>4} (match: {sorted(list(common_tokens))}) -> '{node['summary'][:60]}'{status_mark}")
        hydrated_ids_str = ", ".join(str(s[0]) for s in selected_results)
        print(f"  → Action: Transparently hydrate Step(s) {hydrated_ids_str} into active turn prompt before LLM dispatch.")
        if budget_dropped:
            for step_id, terms, cost in budget_dropped:
                print(f"  [BUDGET DROPPED] Step {step_id} (terms: {terms}, cost: {cost}) dropped — exceeded turn budget.")
    else:
        print("  ✓ No historical identifier collision. Dispatch directly to LLM without hydration.")

    return selected_results

def cmd_hydrate(step_id: int) -> dict:
    """
    Hydrate a cold node into the active prompt.
    Returns the loaded cold node dict.
    
    CONTRACT (D-8 / Invariant 4):
      - 'content' is the ONLY field intended for prompt injection (bounded by policy cap).
      - 'raw_content' preserves verbatim original payload on disk for verification (CONF-01/02).
        Harnesses MUST NOT inject raw_content directly into LLM prompt.
    CONF-02, CONF-05, CONF-06, CONF-09, CONF-10.
    """
    index = load_index()
    policy = load_policy()

    node_meta = index["nodes"].get(str(step_id))
    if node_meta is None:
        node_path = cold_node_path(step_id)
        if os.path.exists(node_path):
            raise OrphanedNodeError(f"Step {step_id} exists on disk as an orphaned cold node but is not indexed in recall_index. (CONF-12 Scenario A)")
        raise NodeNotFoundError(f"Step {step_id} not found in recall index. (CONF-10)")

    node_path = _resolve_cold_node_path(step_id, node_meta)

    # Canonical artifact integrity verification (Y-1)
    if policy.get("integrity_verification", True):
        _, cold_node, _ = verify_artifact(node_path, expected_hash=node_meta["artifact_sha256"])
    else:
        cold_node = load_json(node_path)
    content = cold_node["raw_content"]
    token_estimate = _estimate_tokens(content)

    # Token cap — CONF-06 (W-1: Bounded rehydration enforced at return level)
    max_tokens = policy.get("max_rehydration_tokens_per_call", DEFAULT_POLICY["max_rehydration_tokens_per_call"])
    truncated = False
    if token_estimate > max_tokens:
        truncation_point = max_tokens * 4
        content = content[:truncation_point]
        token_estimate = max_tokens
        truncated = True
        print(f"[WARN] Content truncated to {max_tokens} tokens (policy cap). (CONF-06)")

    print(f"\n[HYDRATE] Step {step_id} — exact verbatim content:")
    print(f"  Token estimate    : {token_estimate}")
    print(f"  payload_sha256    : {cold_node['payload_sha256'][:16]}…")
    print(f"  Scope             : {policy.get('scope', 'single_turn')} (evict after response)")
    print()
    print("─" * 60)
    print(content)
    print("─" * 60)
    print(f"\n[HYDRATE] Eviction trigger: {policy.get('eviction_trigger', 'after_response')}")
    print(f"  → Call `python contextfold.py evict {step_id}` after LLM response to comply with CONF-07.")

    hydrated_result = dict(cold_node)
    hydrated_result["content"] = content          # Bounded prompt payload (CONF-06)
    hydrated_result["raw_content"] = cold_node["raw_content"]  # Verbatim disk payload (CONF-01/02)
    hydrated_result["token_estimate"] = token_estimate
    hydrated_result["truncated"] = truncated

    return hydrated_result

def cmd_evict(step_id: int) -> dict:
    """
    Mark a hydrated node as evicted and record the eviction event in eviction_log.json (Z-5).
    CONF-07: Enforced eviction — no cumulative creep.
    Returns eviction event record dict: {"step_id": step_id, "evicted_at": timestamp} (D-7).
    """
    index = load_index()
    node_meta = index["nodes"].get(str(step_id))

    if node_meta is None:
        raise NodeNotFoundError(f"Step {step_id} not found in recall index.")

    index["nodes"][str(step_id)]["evicted"] = True
    save_index(index)

    evicted_at = _iso_now()
    eviction_event = {"step_id": step_id, "evicted_at": evicted_at}

    # Z-5: Always append to eviction log as a per-turn event
    eviction_log = load_eviction_log()
    eviction_log["evicted"].append(eviction_event)
    save_eviction_log(eviction_log)

    print(f"[EVICT] Step {step_id} evicted from active prompt. Cold storage preserved. (CONF-07)")
    return eviction_event

def cmd_status() -> dict:
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

    return {
        "total": total,
        "active": active_count,
        "evicted": evicted_count,
        "total_tokens": total_tokens
    }

def cmd_validate(exit_on_error: bool = True) -> bool:
    """
    Run integrity check on all cold nodes.
    CONF-02: SHA-256 round-trip.
    CONF-09: Tamper detection via verify_artifact.
    CONF-12: Orphaned nodes (Scenario A) + broken references (Scenario B).
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
        try:
            node_path = _resolve_cold_node_path(step_id, node_meta)
        except PathContainmentError as err:
            msg = f"CONF-12 Path Containment — {err}"
            errors.append(msg)
            print(f"  [FAIL] Step {step_id:>4}: {msg}")
            continue

        if not os.path.exists(node_path):
            msg = f"CONF-12 Scenario B — Broken reference: step {step_id} in index but file missing: {node_path}"
            errors.append(msg)
            print(f"  [FAIL] Step {step_id:>4}: {msg}")
            continue

        if policy.get("integrity_verification", True):
            try:
                verify_artifact(node_path, expected_hash=node_meta.get("artifact_sha256"))
            except IntegrityCheckError as err:
                msg = f"CONF-09 / CONF-12 Scenario C — {err}"
                errors.append(msg)
                print(f"  [FAIL] Step {step_id:>4}: {msg}")
                continue

        pass_count += 1
        print(f"  [ OK ] Step {step_id:>4}: artifact_sha256 verified. (CONF-02 / CONF-09)")

    # 2. Detect orphaned cold nodes (CONF-12 Scenario A)
    if os.path.isdir(COLD_NODE_DIR):
        indexed_steps = {int(s) for s in nodes.keys()}
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
        if exit_on_error:
            sys.exit(0)
        return True
    else:
        print(f"  Validation FAILED — {len(errors)} error(s), {pass_count} passed.")
        print()
        print("  Error summary:")
        for e in errors:
            print(f"    • {e}")
        print()
        if exit_on_error:
            sys.exit(1)
        return False

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

    print("  [1/5] Folding 8 representative turns from a 500-turn session...\n")
    for step_id, summary, raw in DEMO_TURNS:
        print(f"  → Folding step {step_id}: {summary[:60]}")
        cmd_fold(step_id, summary, raw_content=raw)
    print()

    print("  [2/5] Session status after folding:\n")
    cmd_status()

    print("  [3/5] Passive Recall Interception (Software MMU Pattern — CONF-13):")
    cmd_scan("What port did we configure for PostgreSQL?")
    print()
    print("  → Multi-intent showcase query (README §6):")
    cmd_scan("What port are we using for PostgreSQL, and which migration applied the billing table?")
    print()

    print("  [4/5] Pull-based Recall: 'PostgreSQL port' — temporal disambiguation (CONF-04):\n")
    cmd_recall("PostgreSQL port")

    print("  [5/5] Bounded rehydration + eviction cycle (CONF-07):\n")
    print("  → Hydrating step 137 (latest PostgreSQL port state)...")
    try:
        cmd_hydrate(137)
    except (IntegrityCheckError, NodeNotFoundError) as e:
        print(f"  [ERROR] {e}")
        return
    print()
    print("  → LLM response generated. Now evicting step 137 from active prompt...")
    cmd_evict(137)

    print()
    print("  [VALIDATE] Running full integrity check...\n")
    # Z-4 refactor: exit_on_error=False so demo reaches "Demo complete"
    valid = cmd_validate(exit_on_error=False)

    if valid:
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
  python contextfold.py fold     <step_id> <"summary"> [--file path] [--files f1,f2] [--commands c1,c2] [--errors e1,e2]
  python contextfold.py scan     <"prompt">               Passively scan prompt (CONF-13 MMU)
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
                print("Usage: python contextfold.py fold <step_id> <summary> [--file path] [--files f1,f2] [--commands c1,c2] [--errors e1,e2]")
                sys.exit(1)
            step_id = int(args[1])
            summary_parts = []
            files = None
            commands = None
            errors = None
            raw_content = None

            i = 2
            while i < len(args):
                if args[i] == "--file" and i + 1 < len(args):
                    with open(args[i+1], "r", encoding="utf-8") as f:
                        raw_content = f.read()
                    i += 2
                elif args[i] == "--files" and i + 1 < len(args):
                    files = [x.strip() for x in args[i+1].split(",") if x.strip()]
                    i += 2
                elif args[i] == "--commands" and i + 1 < len(args):
                    commands = [x.strip() for x in args[i+1].split(",") if x.strip()]
                    i += 2
                elif args[i] == "--errors" and i + 1 < len(args):
                    errors = [x.strip() for x in args[i+1].split(",") if x.strip()]
                    i += 2
                else:
                    summary_parts.append(args[i])
                    i += 1

            summary = " ".join(summary_parts)
            cmd_fold(
                step_id=step_id,
                summary=summary,
                raw_content=raw_content,
                files=files,
                commands=commands,
                errors=errors
            )

        elif command == "scan":
            if len(args) < 2:
                print("Usage: python contextfold.py scan <prompt>")
                sys.exit(1)
            prompt = " ".join(args[1:])
            cmd_scan(prompt)

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
            cmd_validate(exit_on_error=True)

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

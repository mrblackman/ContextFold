# IPCF-1.1 Conformance Test Suite & Verification Matrix
### Normative Requirements for Compliant In-Place Context Folding Implementations

**Specification ID:** `IPCF-1.1`  
**Document ID:** `CONFORMANCE-IPCF-001`  
**Status:** Normative Test Suite  
**Repository:** [github.com/mrblackman/ContextFold](https://github.com/mrblackman/ContextFold)  

---

## 🎯 1. Overview & Purpose

To claim conformance with the **In-Place Context Folding Protocol (IPCF-1.1)**, a runtime engine or IDE implementation (such as Google Antigravity, Cursor, Windsurf, or open-source agent harnesses) MUST satisfy the **12 normative test requirements** detailed below.

IPCF moves beyond qualitative promises ("we compress context") into **empirically falsifiable engineering invariants**:

```text
Specification (IPCF-1.1) ──► Schemas ──► Reference Engine ──► Conformance Test Suite (Pass/Fail)
```

> **Foundational concept:** IPCF distinguishes between two hash identities for every folded artifact — `projection_sha256` (deterministic logical identity) and `artifact_sha256` (full on-disk identity including runtime metadata). See **Section 2: Normative Projection Scope** before reading CONF-11 and CONF-12.

---

## 📐 2. Normative Identity Model — Three Hash Identities

Every folded artifact carries **three distinct hash identities**. Implementations MUST compute and store all three. Each answers a different question about the artifact:

```text
                    ContextFold Identity Model
                              │
             ┌────────────────┬────────────────┐
             │                │                │
       payload_sha256   projection_sha256   artifact_sha256
             │                │                │
       "Ne geldi?"       "Ne üretildi?"    "Nasıl saklandı?"
             │                │                │
          CONF-02          CONF-11          CONF-09
        Round-Trip       Deterministic       Tamper
                          Replay             Detection
```

### 2.0. Lifecycle Semantics Table

| Identity | Source | Runtime metadata’dan etkilenir mi? | Lifecycle boyunca değişebilir mi? |
| :--- | :--- | :---: | :---: |
| `payload_sha256` | Raw source content | No | No |
| `projection_sha256` | Canonical deterministic projection | No | No |
| `artifact_sha256` | Serialized cold node artifact (full file) | Yes | Yes (if serialization changes) |

> **Critical:** `artifact_sha256` is **not** a logical identity. It is the physical serialization identity of the cold node file. The following is a **valid and expected** state in IPCF:
> ```text
> same payload + same projection + different serialization timestamp
>       ↓
> same payload_sha256
> same projection_sha256
> different artifact_sha256   ← NOT a failure
> ```
> This distinction ensures format migrations do not invalidate existing logical identities.

---

### 2.1. `payload_sha256` — Immutable Content Identity

`payload_sha256` is the SHA-256 of the **raw source content** as it existed before folding. It MUST remain identical through the entire fold → store → hydrate lifecycle. It is the hash that CONF-02 (Round-Trip) tests.

```text
payload_sha256  =  SHA-256(raw_source_content, UTF-8 encoded)
```

`payload_sha256` MUST NOT include any metadata fields. It is computed solely from the content that the turn produced, before any IPCF processing.

### 2.2. `projection_sha256` — Deterministic Logical Identity

`projection_sha256` represents the **runtime-independent, reproducible logical artifact identity**. It is the hash that CONF-11 (Deterministic Replay) tests.

**`projection_sha256` MUST be computed exclusively from the following fields, in the following canonical order:**

| Field | Notes |
| :--- | :--- |
| `projection_version` | Schema/algorithm version string (e.g. `"ipcf-projection-v1"`). Changing the projection algorithm MUST increment this version, ensuring old and new outputs are never compared as equal. |
| `step_id` | Canonical turn index sourced from the input conversation history. See constraint below. |
| `normalized_content` | Source content normalized to UTF-8, NFC, with trailing whitespace stripped. |
| `identifiers` | Sorted lexicographically. No set/dict order dependence. |
| `files` | Sorted lexicographically by path. |
| `commands` | Sorted lexicographically. |
| `errors` | Sorted lexicographically. |
| `folding_policy_hash` | SHA-256 of the canonical JSON serialization of the active `HydrationPolicy` (sorted keys, no whitespace). |

#### Canonical `step_id` Constraint (Normative)

> `step_id` MUST be sourced from the canonical turn index of the input conversation history. It MUST NOT be assigned by the runtime engine as an auto-increment counter.

```text
Conversation History
        │
        ├── Turn 136
        ├── Turn 137  ← step_id = 137  (from history, not runtime)
        ├── Turn 138
        │
        ↓
    ContextFold fold(step_id=137, ...)
```

A runtime engine that assigns `step_id` as `fold() → 1, fold() → 2, fold() → 3` is **non-conformant**. Two independent fold runs on the same history MUST produce the same `step_id` values, because those values are determined by the history, not by the engine.

**`projection_sha256` MUST NOT include any of the following:**

| Excluded Field | Reason |
| :--- | :--- |
| `folded_at` | Wall-clock timestamp — non-deterministic across runs. |
| `created_at` | Same. |
| `datetime.now()` / `time.time()` | Same. |
| Random UUIDs | Non-deterministic by definition. |
| Process ID / hostname | Machine-specific. |
| Absolute filesystem paths | Machine-specific. |
| Nondeterministically ordered collections | Platform-dependent iteration order. |
| Session-level runtime metadata | Belongs to `artifact_sha256`. |

### 2.3. `artifact_sha256` — Canonical Serialization Integrity

`artifact_sha256` is the SHA-256 hash of the **canonical JSON serialization of the cold node, excluding the `artifact_sha256` field itself**. It is the hash that CONF-09 (Tamper Detection) tests.

> **Self-Reference Constraint (Normative):** A file cannot contain the SHA-256 of itself. `artifact_sha256` MUST be computed over the canonical serialization with the `artifact_sha256` field absent (or set to `null`), then written into the file as a second step.

#### Write Protocol (Two-Phase):

```text
Phase 1 — Compute:
  1. Construct the cold node object (all fields populated).
  2. Set artifact_sha256 = null (or omit the field).
  3. Serialize to canonical JSON (sorted keys, no extra whitespace, UTF-8).
  4. artifact_sha256 = SHA-256(canonical_bytes).

Phase 2 — Persist (atomic):
  5. Set artifact_sha256 in the cold node object.
  6. Write final JSON to disk (via temp-file-then-rename).
```

#### Verification Protocol (Read):

```text
  1. Read cold node JSON from disk.
  2. Extract and save stored_hash = artifact_sha256.
  3. Set artifact_sha256 = null in the object.
  4. Serialize to canonical JSON (same rules as write).
  5. computed_hash = SHA-256(canonical_bytes).
  6. PASS if stored_hash == computed_hash. FAIL raises IntegrityCheckError.
```

> **Note:** "Canonical JSON" means: keys sorted lexicographically, no trailing whitespace, no indentation variation. Implementations MUST document their canonical serialization rule to ensure interoperability.

### 2.4. Version Isolation Guarantee

If the projection algorithm changes between `projection-v1` and `projection-v2`, the `projection_sha256` values MUST differ even for identical source content. This prevents cross-version false equality:

```text
IPCF-1.1 / projection-v1  →  projection_sha256 = X
IPCF-1.1 / projection-v2  →  projection_sha256 = Y   (Y ≠ X, by design)
```

Implementations MUST embed `projection_version` in the cold node schema and in the recall index entry.

---

## 📋 3. Normative Conformance Matrix (12 Core Tests)

| ID | Test Name | Invariant Tested | Requirement | Pass Criteria |
| :--- | :--- | :--- | :--- | :--- |
| **`CONF-01`** | **Lossless Cold Storage** | Invariant 1 (Storage) | Folded turn payloads written to disk must preserve 100% of original bytes. | Byte count and content match verbatim. No silent truncation. |
| **`CONF-02`** | **Payload Round-Trip** | Invariant 1 (Lossless) | `payload_sha256(source_content) == payload_sha256(stored_in_cold_node) == payload_sha256(hydrated_content)`. Raw source content must survive fold → store → hydrate unchanged. | Bit-for-bit `payload_sha256` equality across the complete lifecycle. `artifact_sha256` MAY differ across runs — this is NOT a failure. (See Section 2.1.) |
| **`CONF-03`** | **Deterministic Addressing**| Invariant 2 (Determinism)| Given identical conversation history, the extracted index must be bit-for-bit identical. | No nondeterministic sorting, timestamp drift, or random entity omissions. |
| **`CONF-04`** | **Temporal Disambiguation** | Addressing Layer | When an entity evolves across turns (e.g., Port 5433 → 5434 → 5433), queries must rank candidates chronologically. | Engine returns sorted candidate steps with latest state clearly distinguished. |
| **`CONF-05`** | **Exact Verbatim Retrieval**| Invariant 3 (Retrieval) | Historical payload retrieved from cold storage must bypass LLM re-interpretation. | Direct I/O stream from disk. Zero hallucinated parameters. |
| **`CONF-06`** | **Bounded Rehydration Cap** | Invariant 4 (Bounded) | On-demand hydration cannot exceed `max_rehydration_tokens` (reference default: 3000 tokens; implementors MAY override with a documented conformance profile). | Calls exceeding the configured limit are chunked or bounded with an explicit truncation warning. The cap MUST exist; the threshold is policy-defined. |
| **`CONF-07`** | **Enforced Eviction Cycle** | Invariant 4 (Eviction) | Recalled node tokens MUST be evicted from the prompt on the subsequent turn. | Context size before hydration `S_0` returns to `S_0 + δ_new` on turn `N+1`. No cumulative creep (`8k → 10k → 8k`). |
| **`CONF-08`** | **Dual-Projection Isolation**| Dual-Projection Model | Viewing cold nodes in the Developer UI side-drawer must NOT inject tokens into LLM prompt. | Model token counter unchanged when developer inspects raw diffs. |
| **`CONF-09`** | **Tamper Detection** | Security Guardrail | Modification of the cold node file on disk MUST be detected via `artifact_sha256` mismatch. `payload_sha256` and `projection_sha256` integrity are independently verifiable. | `artifact_sha256` mismatch raises explicit `IntegrityCheckError`; corrupted node is never hydrated. (See Section 2.3.) |
| **`CONF-10`** | **Explicit Failure Mode** | Robustness | Querying a non-existent step or missing file must fail explicitly. | Engine returns `NodeNotFoundError`; LLM is strictly prevented from hallucinating fallback data. |
| **`CONF-11`** | **Deterministic Replay** | Invariant 2 (Determinism) | Given identical input history, identical folding policy, and identical `projection_version`: `projection_sha256(Run A) == projection_sha256(Run B)`. Runtime metadata (`folded_at`, session timestamps) MUST NOT affect the projection hash. | `projection_sha256` is bit-for-bit identical across two independent runs on the same input. `artifact_sha256` MAY differ due to runtime metadata — this is NOT a failure. (See Section 2.2.) |
| **`CONF-12`** | **Inconsistent State Detection** | Robustness + Integrity | A fold operation interrupted mid-execution may produce orphaned nodes, broken references, or SHA mismatches. The engine MUST detect each broken state, refuse to consume it, and report it explicitly. **Automatic repair is NOT required by IPCF-1.1.** | Engine raises `OrphanedNodeError`, `BrokenReferenceError`, or `IntegrityCheckError` for each respective broken state. Corrupted or incomplete data is never silently consumed. (See Section 4.4.) |

---

## 🔬 4. Deep Dive: Key Conformance Scenarios

### 4.1. Stress Test `CONF-04`: Temporal Disambiguation (The PostgreSQL Port Scenario)
In long-horizon agentic workflows, configurations evolve, get reverted, and re-applied:

```text
Turn 37:  PostgreSQL configured on Port 5433
Turn 91:  PostgreSQL switched to Port 5434 (Conflict resolution)
Turn 137: Port 5434 reverted back to Port 5433
Turn 318: Migration V12 applied
Turn 342: Migration V12 rolled back due to deadlocks
Turn 447: Migration V12 patched and re-applied
```

#### Conformance Requirement:
When the model executes `recall_archived_nodes(query="PostgreSQL port")`:
1. The engine MUST NOT return an arbitrary single match.
2. The engine MUST return a chronological candidate array:
   ```json
   {
     "query": "PostgreSQL port",
     "candidates": [
       { "step_id": 137, "timestamp": "...", "value": "5433", "status": "latest_chronological" },
       { "step_id": 91,  "timestamp": "...", "value": "5434", "status": "superseded" },
       { "step_id": 37,  "timestamp": "...", "value": "5433", "status": "historical" }
     ]
   }
   ```
3. This allows the model to correctly reason: *"Port 5433 was initially assigned, temporarily changed to 5434 in step 91, and reverted to 5433 in step 137."*

---

### 4.2. Stress Test `CONF-07`: Bounded Rehydration Under Rapid-Fire Recalls
An adversary or confused agent attempts multiple consecutive recalls in turns 100 to 105:

```text
Turn 100: Active Prompt = 8,100 tokens. (Invokes recall Step 37 -> +1,800 tokens)
Turn 100 Response Generated.
Turn 101: Active Prompt MUST return to ~8,150 tokens. (Invokes recall Step 91 -> +2,100 tokens)
Turn 101 Response Generated.
Turn 102: Active Prompt MUST return to ~8,200 tokens. (Invokes recall Step 318 -> +2,500 tokens)
```

#### Pass Criteria:
If prompt token count accumulates linearly (`8.1k → 9.9k → 12.0k → 14.5k`), the engine **FAILS CONF-07**. The runtime must enforce **post-turn eviction**.

---

### 4.3. Stress Test `CONF-11`: Deterministic Replay

A fold operation run twice on the **identical input conversation** with the **identical policy** and **identical `projection_version`** must produce the **identical `projection_sha256`**, even though `artifact_sha256` and `folded_at` may differ.

```text
Run A: input=history_v7.json, policy=default, projection_version=ipcf-projection-v1
       → projection_sha256 = abc123   artifact_sha256 = XYZ   folded_at = 18:31:02

Run B: input=history_v7.json, policy=default, projection_version=ipcf-projection-v1
       → projection_sha256 = abc123   artifact_sha256 = QRS   folded_at = 18:31:07

CONF-11 result: PASS  (projection_sha256 matches; artifact_sha256 difference is expected)
```

#### Common Failure Causes (all are CONF-11 FAILs):
- `uuid.uuid4()` used as an index key or projection field → non-deterministic.
- `datetime.now()` / `folded_at` included in the projection scope → timestamp drift.
- `dict` / `set` iteration without `sorted()` → platform-dependent ordering.
- Parallel workers introducing race-condition ordering in index fields.
- `projection_version` not included in projection → algorithm changes invisible to hash.

#### Pass Criteria:
`projection_sha256(Run A) == projection_sha256(Run B)`. Any difference is a **CONF-11 FAIL**.
`artifact_sha256(Run A) != artifact_sha256(Run B)` due to runtime metadata — this is **NOT a failure**.

---

### 4.4. Stress Test `CONF-12`: Inconsistent State Detection

A fold operation has two critical write phases: **(1) write cold node to disk** and **(2) update recall index**.
If the process is killed (SIGKILL, power loss, disk full) between these phases, three broken states can emerge:

```text
Scenario A — Orphaned Node:
  cold_node/step_042.json  ✔ written
  recall_index.json        ✗ NOT updated (no reference to step_042)
  → System has data that can never be found. Silent data leak.

Scenario B — Broken Reference:
  recall_index.json        ✔ updated (references step_042)
  cold_node/step_042.json  ✗ NOT written (file missing)
  → Recall call triggers BrokenReferenceError.

Scenario C — Tampered / Partial Write:
  cold_node/step_042.json  ✔ written (but truncated mid-byte)
  recall_index.json        ✔ updated (references old artifact_sha256)
  → SHA mismatch on hydration. IntegrityCheckError.
```

#### IPCF-1.1 Normative Requirement: Detect, Refuse, Report

> **The implementation MUST detect, refuse to consume, and explicitly report each inconsistent persistent state. Automatic repair is NOT required by IPCF-1.1.**

```text
Consistent state  →  consume

Orphaned node
Broken reference
SHA mismatch      →  DETECT → REFUSE → REPORT (explicit error)

                      ↕ NOT required by IPCF-1.1 ↕
                      DETECT → REPAIR → RESUME
```

A future **Repair Protocol** extension MAY define automatic recovery behavior. Until then, any implementation that silently repairs or silently ignores inconsistent state **FAILS CONF-12**.

#### Pass Criteria:
- **Scenario A:** Engine MUST detect orphaned cold nodes on startup or integrity scan, raise `OrphanedNodeError`, and refuse to hydrate the node.
- **Scenario B:** Engine MUST raise `BrokenReferenceError` with the missing file path; MUST NOT hallucinate content.
- **Scenario C:** Engine MUST raise `IntegrityCheckError`; corrupted node MUST NOT be hydrated into prompt.

> **Implementation note:** `write-to-temp-then-rename` achieves filesystem-level write atomicity for individual files. It does NOT solve the multi-artifact transaction problem (node file + index file). IPCF-1.1 does not mandate a specific mechanism; it mandates the failure behavior above.

---

## 🛡️ 5. Conformance Test Verification Harness

Compliant implementations should provide an automated test runner executing this suite. A reference test script structure:

```bash
# Execute official IPCF-1.1 conformance tests
python -m unittest tests/test_ipcf_conformance.py
```

Expected output:
```text
test_conf01_lossless_storage (tests.test_ipcf_conformance) ... ok
test_conf02_sha256_roundtrip (tests.test_ipcf_conformance) ... ok
test_conf03_deterministic_indexing (tests.test_ipcf_conformance) ... ok
test_conf04_temporal_disambiguation (tests.test_ipcf_conformance) ... ok
test_conf05_verbatim_retrieval (tests.test_ipcf_conformance) ... ok
test_conf06_bounded_rehydration_cap (tests.test_ipcf_conformance) ... ok
test_conf07_enforced_eviction_cycle (tests.test_ipcf_conformance) ... ok
test_conf08_dual_projection_isolation (tests.test_ipcf_conformance) ... ok
test_conf09_tamper_detection (tests.test_ipcf_conformance) ... ok
test_conf10_explicit_failure_modes (tests.test_ipcf_conformance) ... ok
test_conf11_deterministic_replay (tests.test_ipcf_conformance) ... ok
test_conf12_crash_recovery_atomic_fold (tests.test_ipcf_conformance) ... ok

----------------------------------------------------------------------
Ran 12 tests in 0.521s

OK (IPCF-1.1 Fully Compliant)
```

---

## 📜 6. Licensing & Trademark

The IPCF Conformance Test Specification is published under the **[MIT License](LICENSE)** to foster universal interoperability across the autonomous AI agent ecosystem.

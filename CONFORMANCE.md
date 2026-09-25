# IPCF-1.1 Conformance Test Suite & Verification Matrix
### Normative Requirements for Compliant In-Place Context Folding Implementations

**Specification ID:** `IPCF-1.1`  
**Document ID:** `CONFORMANCE-IPCF-001`  
**Status:** Normative Test Suite  
**Repository:** [github.com/mrblackman/ContextFold](https://github.com/mrblackman/ContextFold)  

---

## 🎯 1. Overview & Purpose

To claim conformance with the **In-Place Context Folding Protocol (IPCF-1.1)**, a runtime engine or IDE implementation (such as Google Antigravity, Cursor, Windsurf, or open-source agent harnesses) MUST satisfy the **10 normative test requirements** detailed below.

IPCF moves beyond qualitative promises ("we compress context") into **empirically falsifiable engineering invariants**:

```text
Specification (IPCF-1.1) ──► Schemas ──► Reference Engine ──► Conformance Test Suite (Pass/Fail)
```

---

## 📋 2. Normative Conformance Matrix (10 Core Tests)

| ID | Test Name | Invariant Tested | Requirement | Pass Criteria |
| :--- | :--- | :--- | :--- | :--- |
| **`CONF-01`** | **Lossless Cold Storage** | Invariant 1 (Storage) | Folded turn payloads written to disk must preserve 100% of original bytes. | Byte count and content match verbatim. No silent truncation. |
| **`CONF-02`** | **SHA-256 Round-Trip** | Invariant 3 (Integrity) | `Hash(Original) == Hash(ColdNode) == Hash(Hydrated)`. | Bit-for-bit SHA-256 equality across the entire lifecycle. |
| **`CONF-03`** | **Deterministic Addressing**| Invariant 2 (Determinism)| Given identical conversation history, the extracted index must be bit-for-bit identical. | No nondeterministic sorting, timestamp drift, or random entity omissions. |
| **`CONF-04`** | **Temporal Disambiguation** | Addressing Layer | When an entity evolves across turns (e.g., Port 5433 → 5434 → 5433), queries must rank candidates chronologically. | Engine returns sorted candidate steps with latest state clearly distinguished. |
| **`CONF-05`** | **Exact Verbatim Retrieval**| Invariant 3 (Retrieval) | Historical payload retrieved from cold storage must bypass LLM re-interpretation. | Direct I/O stream from disk. Zero hallucinated parameters. |
| **`CONF-06`** | **Bounded Rehydration Cap** | Invariant 4 (Bounded) | On-demand hydration cannot exceed `max_rehydration_tokens` (reference default: 3000 tokens; implementors MAY override with a documented conformance profile). | Calls exceeding the configured limit are chunked or bounded with an explicit truncation warning. The cap MUST exist; the threshold is policy-defined. |
| **`CONF-07`** | **Enforced Eviction Cycle** | Invariant 4 (Eviction) | Recalled node tokens MUST be evicted from the prompt on the subsequent turn. | Context size before hydration `S_0` returns to `S_0 + Δ_new` on turn `N+1`. No cumulative creep (`8k -> 10k -> 8k`). |
| **`CONF-08`** | **Dual-Projection Isolation**| Dual-Projection Model | Viewing cold nodes in the Developer UI side-drawer must NOT inject tokens into LLM prompt. | Model token counter unchanged when developer inspects raw diffs. |
| **`CONF-09`** | **Tamper Detection** | Security Guardrail | Modified on-disk payload must trigger an integrity failure. | Checksum mismatch raises explicit `IntegrityCheckError`; corrupted data never hydrated. |
| **`CONF-10`** | **Explicit Failure Mode** | Robustness | Querying a non-existent step or missing file must fail explicitly. | Engine returns `NodeNotFoundError`; LLM is strictly prevented from hallucinating fallback data. |
| **`CONF-11`** | **Deterministic Replay** | Invariant 2 (Determinism) | `Hash(same_input_history) + Hash(same_policy) → Hash(same_output_recall_index)`. Given identical inputs and policy, fold output must be bit-for-bit reproducible. | SHA-256 of the generated recall index is identical across two independent runs on the same input. No timestamp drift, UUID randomness, or sort nondeterminism in index content. |
| **`CONF-12`** | **Crash Recovery / Atomic Fold** | Robustness + Integrity | A fold operation interrupted mid-execution must not leave the system in a partially-written or broken state. | Engine detects orphaned cold nodes (cold node written, index not updated), broken references (index entry exists, file missing), and SHA mismatch (tampered node). Each case must produce an explicit error; partial state must never be silently accepted. |

---

## 🔬 3. Deep Dive: Key Conformance Scenarios

### 3.1. Stress Test `CONF-04`: Temporal Disambiguation (The PostgreSQL Port Scenario)
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

### 3.2. Stress Test `CONF-07`: Bounded Rehydration Under Rapid-Fire Recalls
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

### 3.3. Stress Test `CONF-11`: Deterministic Replay

A fold operation run twice on the **identical input conversation** with the **identical policy** must produce the **identical recall index**, byte-for-byte.

```text
Run A: input=history_v7.json, policy=default → recall_index_A.json  (SHA-256: abc123)
Run B: input=history_v7.json, policy=default → recall_index_B.json  (SHA-256: abc123)
```

#### Common Failure Causes (all are FAILS):
- `uuid.uuid4()` called during index generation → non-deterministic node IDs.
- `datetime.now()` embedded in index keys → timestamp drift between runs.
- `dict` / `set` iteration without sorted() → platform-dependent ordering.
- Parallel workers introducing race-condition ordering.

#### Pass Criteria:
`SHA-256(recall_index_A) == SHA-256(recall_index_B)`. Any difference is a **CONF-11 FAIL**.

---

### 3.4. Stress Test `CONF-12`: Crash Recovery / Atomic Fold

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
  → Recall call triggers NodeNotFoundError.

Scenario C — Tampered / Partial Write:
  cold_node/step_042.json  ✔ written (but truncated mid-byte)
  recall_index.json        ✔ updated (references old SHA-256)
  → SHA mismatch on hydration. IntegrityCheckError.
```

#### Pass Criteria:
- **Scenario A:** Engine MUST detect orphaned cold nodes on startup or integrity scan, log the orphan ID, and refuse to hydrate it.
- **Scenario B:** Engine MUST return `NodeNotFoundError` with the broken reference path; MUST NOT hallucinate content.
- **Scenario C:** Engine MUST raise `IntegrityCheckError`; corrupted node MUST NOT be hydrated into prompt.

> **Note:** Atomic fold can be implemented via write-to-temp-then-rename (filesystem atomicity) or a two-phase commit log. The mechanism is implementation-defined; the failure behavior above is normative.

---

## 🛡️ 4. Conformance Test Verification Harness

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

## 📜 5. Licensing & Trademark

The IPCF Conformance Test Specification is published under the **[MIT License](LICENSE)** to foster universal interoperability across the autonomous AI agent ecosystem.

# proj_doc_builder

**Turn a codebase into documentation an AI can actually read.** One command to build the outline,
one command to load it.

> **Language**: **English** (default) · [中文](README.zh-CN.md)
>
> `README.zh-CN.md` is a synced translation — update both together.
>
> Ships as a **DeepSeek Harness bundle** (`@walvka-code/proj-doc-builder`) **and** as a plain CLI.

```bash
# The skill directory: SKILL.md + tools/ live here
SKILL="$(pwd)/skills/proj-doc-builder"

# Sync: build/refresh the documentation index (preview first, then publish)
python "$SKILL/tools/sync_project_docs.py" . --dry-run --json
python "$SKILL/tools/sync_project_docs.py" . --apply --json

# Load: give a fresh conversation an entry point (overview → one document)
python "$SKILL/tools/load_docs.py" . --json
python "$SKILL/tools/load_docs.py" . --mode target --target src/core --json
```

What it does:

1. Keeps **one Markdown outline per source directory** (file inventory, key symbols, dependencies,
   role in the system).
2. Remembers "what the tree looked like last time" via **content hashes + a manifest**, so the next
   sync only touches directories that **actually changed**.
3. Publishes as a **recoverable transaction**; retiring a document is an explicit, evidence-gated
   decision — never an automatic cleanup.

The stance, in one sentence: **"docs are in sync" must be backed by a manifest the tooling wrote,
not by the model that produced the prose.**

---

## Contents

- [Install](#install)
- [Repository layout](#repository-layout)
- [The two problems it solves](#the-two-problems-it-solves)
- [Guarantees](#guarantees)
- [The six-phase workflow](#the-six-phase-workflow)
- [Data model](#data-model)
- [Using the loader](#using-the-loader)
- [Platform support](#platform-support)
- [Tests](#tests)
- [Limitations and trade-offs](#limitations-and-trade-offs)
- [License](#license)

---

## Install

The repository is a **DeepSeek Harness bundle** that packages the skill, its Python tools and its
tests. Pick the install that fits your agent:

**Option 1 — DeepSeek Harness bundle (recommended)**

Add this market index under **Plugins → More → Custom plugin markets → Discover**, then install
`proj-doc-builder`:

```text
https://walvka-code.github.io/proj-doc-builder/index.json
```

(The same file is also served from `https://raw.githubusercontent.com/walvka-code/proj-doc-builder/main/index.json`,
but that host is unreachable from some networks; GitHub Pages is the primary address.)

Without the market, pin one commit (the bundle is a package directory in Git):

```bash
git+https://github.com/walvka-code/proj-doc-builder.git#<40-hex-commit>
```

or install a local checkout with the `plugin_manager` tool:

```json
{ "action": "install_bundle", "target": "C:/path/to/proj-doc-builder" }
```

The bundle mounts one bundled skill provider (rank 600): the skill is available in **every** session
of the profile, and a project-local `.dsh/skills/proj-doc-builder/` copy still wins when a repository
wants to pin its own version.

**Option 2 — as a project skill**

Copy `skills/proj-doc-builder/` to
`<project>/.dsh/skills/proj-doc-builder/`. `SKILL.md` resolves its tools through the skill directory
the host reports, so both layouts work unchanged.

**Option 3 — as a plain CLI** (no agent framework needed)

```bash
git clone <this-repo> proj-doc-builder
SKILL="$(pwd)/proj-doc-builder/skills/proj-doc-builder"
python "$SKILL/tools/sync_project_docs.py" /path/to/your/project --dry-run --json
```

**Python 3.12+, zero third-party runtime dependencies** (standard library only).
Tests need `pytest` (see `requirements-dev.txt`).

Prerequisites: your project root needs a `project_docs/` directory and a `CLAUDE.md` entry point.

---

## Repository layout

```text
proj-doc-builder/
├── package.json                        # name / dsh.bundle.patch / engines
├── cordis.patch.yml                    # mounts the bundled skill provider
├── index.js                            # reads SKILL.md, registers the skill
├── index.json                          # plugin market index (format 1)
├── icon.svg / locale/                  # bundle display metadata
├── skills/proj-doc-builder/            # SKILL.md + tools/ + references/ + tests/
├── scripts/check-bundle.mjs            # offline bundle checks (CI)
└── .github/workflows/ci.yml
```

---

## The two problems it solves

| # | Problem | What this repo gives you |
|---|---|---|
| **1** | You want to map an unfamiliar codebase quickly, and keep a baseline for "what changed since" | **Outline**: `project_docs/**/*.md`, one document per source directory. **Baseline**: `_scan_state.json` (per-file SHA-256 + directory fingerprints) plus the manifest's `generation_id` (content-only hash). Together they make the next sync **incremental** instead of a full rewrite. |
| **2** | You want a new AI conversation to load the project shape instead of re-reading source from scratch | **The loader ships in this repo**: `tools/load_docs.py` first verifies the manifest against the documents (per-file hashes + complete-set check), then serves either an **overview** (tree + metadata + section names) or a **single document / single section**. Writer and reader import the same contract, so the rules cannot drift apart. |

Why not just "have a model write a pile of `.md` files"? Because **the producer and the verifier must
be separate actors** — otherwise "the model says it's in sync" can never be falsified.

| Failure mode | Symptom | What this tool does |
|---|---|---|
| Drift | Source gained a file; the document's inventory is permanently one row short | Content-hash snapshot + worklist; only changed directories are rewritten |
| Unfalsifiable claims | "It's synced" with nothing to check | An independent manifest records size/SHA-256/title/status per document; mismatch is refused |
| Mass rewrite destroys history | Everything is rewritten just to pass validation | Only documents for changed directories block; unchanged history is audited, not rewritten |

---

## Guarantees

| Guarantee | Mechanism |
|---|---|
| **No writes by default** | Without `--apply` everything is report-only; staging lives in the system temp dir, never in the project tree |
| **Single commit point** | The atomic replace of `project_docs/_manifest.json` is the only documentation-generation commit point |
| **Never clobbers concurrent writes** | Publication uses no-replace rename (Windows `os.rename`, Linux `renameat2(RENAME_NOREPLACE)`); a mismatched preimage aborts |
| **Failures are classified** | Only `NOT_INSTALLED` / `INSTALLED` / `UNKNOWN` / `PARTIALLY_INSTALLED` — never a fabricated "committed" |
| **Working copy stays clean** | Locks, transaction journals and CAS artifacts live in the temp dir; your VCS never sees them |
| **Deletion is a burden of proof** | A document is only quarantined when its source directory is proven gone, it has zero inbound links, carries the exact marker and is registered — and even then it is report-only by default |
| **Idempotent** | A zero-change run reports `planned_writes == []` and does not refresh `last_synced` |

---

## The six-phase workflow

| Phase | Action | Key output |
|---|---|---|
| 0 | Preflight: project root, `project_docs/`, `CLAUDE.md`, no pending recovery | — |
| 1 | `--dry-run` yields change classes and a worklist | `scan.worklist.scaffold` (profile + precomputed relative links) |
| 2 | The **producer** writes prose per worklist entry and `--strict` validates each one | `project_docs/*.md` |
| 3 | Whole-tree dry-run (links, blockers, manifest coverage) | `validation.blocking_failures == []` |
| 4 | Retirement report: documents whose source is gone and unreferenced | `phase4.candidates` (report-only by default) |
| 5 | `--apply` publishes | manifest / CLAUDE tree / scan state |
| 6 | Terminal verification + read-only VCS scope check | `planned_writes == []` |

The coordinator **never generates natural language**: it validates and publishes; the prose is
written by the producer (a human or a model).

Exit codes: `0` all gates passed / `1` a gate failed (including "a transaction needs recovery") /
`2` input or runtime error / `3` publish failed (**not the same as "not published"** — read
`publish.disposition`).

---

## Data model

| File | Purpose |
|---|---|
| `project_docs/_manifest.json` | Schema v2; per document `path/depth/title/status/bytes/sha256/synced_at`, plus `generation_id` (content-only, timestamps excluded) and `depth_counts` |
| `project_docs/_scan_state.json` | Source-tree snapshot (per-file SHA-256 + directory fingerprints): **this is the change-tracking baseline** that produces the next worklist |
| Six marker groups in `CLAUDE.md` | `doc-tree-instruction` / `project-overview` / `common-commands` / `architecture` / `test-structure` / `dependencies`. The tool **replaces only the first group**; the rest are only checked for unique, paired, non-overlapping markers |

---

## Using the loader

```bash
# Overview: tree + per-document title/status/bytes/section names
# (defaults: 80 documents, 6 section names each)
python tools/load_docs.py . --json
python tools/load_docs.py . --limit 0 --sections 0 --json   # every document, metadata only

# Target: a source directory, a document path, or a project-relative path
python tools/load_docs.py . --mode target --target src/core --json
python tools/load_docs.py . --mode target --target project_docs/src/core.md --json

# One section only
python tools/load_docs.py . --mode target --target src/core --section Dependencies --json
```

**Verify, then serve.** If the manifest disagrees with the documents (edited files, or an
unregistered document), the loader exits `1` with `manifest-stale` — it will not serve a stale index.

Exit codes: `0` verified and served / `1` stale index or unregistered target / `2` usage or
path-safety error (including `../` traversal).

---

## Platform support

| Platform | Status | Notes |
|---|---|---|
| **Windows** | ✅ Verified | Reparse-point/junction detection, handle-bound deletion (reclaims tombstones), `msvcrt` locks |
| **Linux** | ✅ Verified | `fcntl` locks, `dir_fd` + `O_NOFOLLOW` path walking, `renameat2(RENAME_NOREPLACE)`, directory `fsync` |
| **macOS** | ⚠️ Untested | A `renamex_np` branch exists but CI does not cover it; APFS case-insensitivity and `fsync` semantics are unverified |

Three things to know on Linux:

1. **`renameat2(RENAME_NOREPLACE)` is a hard requirement.** When unsupported, the tool raises
   `NoReplaceRenameUnsupported` and **refuses** to fall back to a replacing rename — a silent
   fallback would quietly break crash consistency.
2. **Directory `fsync` degrades instead of aborting.** Filesystems that reject it are recorded and
   surfaced (`warnings.warn` plus a `durability-degraded` warning in the report); real I/O errors
   still propagate.
3. **On POSIX, tombstones are archived, not deleted.** Retired transaction directories are renamed to
   `.phase4-archive-*` and kept forever (there is no handle-bound `unlink`), so they accumulate and
   need manual cleanup. Windows reclaims them.

---

## Tests

```bash
python -m pytest skills/proj-doc-builder/tests -q   # the Python tools (360 cases)
npm test                                            # the bundled provider + frontmatter reader
npm run check                                       # manifest, patch, packaged files, market index
```

**360 tests** (Windows: 354 passed / 6 skipped; on Linux only the Windows-handle-semantics tests
flip to skipped). Everything runs in `tmp_path` — your real `project_docs/` is never touched. They
pin **crash consistency**, not features:

- a failure before the manifest commit must roll back completely; after it, only
  `installed/partial/unknown` may be reported and rollback is forbidden
- `KeyboardInterrupt` injected before and after every rename
- forged journal slots, ABA races, documents modified concurrently during staging
- unavailable `renameat2` → typed failure; rejected directory `fsync` → visible degradation
  (CI runs a dedicated container/overlayfs job for both)

---

## Limitations and trade-offs

- Journal and CAS artifact directories must live on **the same filesystem** as the project root
  (cross-volume setups make `--apply` refuse).
- Path whitelist: relative POSIX, lowercase `.md` only; absolute paths, drive letters, backslashes,
  `..`, ADS, reserved device names and case-only conflicts are rejected.
- **Symlinks, junctions and reparse points are never followed**; when path identity cannot be proven,
  the tool fails closed.
- `--full` is only the scanner's `force_full` switch — it is **not** "reconcile every document's file
  inventory"; that remains a producer step.
- Document retirement is a **burden-of-proof flow**, not a cleanup utility: report-only by default,
  `--apply` requires explicit authorization.

---

## License

[MIT](LICENSE)

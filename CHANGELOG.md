# Changelog

All notable changes to this project are recorded here. The skill version
(`SKILL.md` frontmatter), the manifest producer version and the package version
move together.

## 4.0.0 — 2026-11-05

The project is renamed from `sync-project-docs` to **proj_doc_builder**, and the
bundle now lives at the repository root instead of `packages/sync-project-docs/`.

### Breaking

- **Skill name**: `sync-project-docs` → `proj-doc-builder`. A project-level copy
  under `.dsh/skills/sync-project-docs/` is no longer shadowing this bundle; move
  or delete it.
- **Scan state**: the identity strings written into `project_docs/_scan_state.json`
  changed with the name (`kind`, producer, hash domains). A file written by 3.x is
  rejected with a scan-state error — delete it and run one full scan to re-baseline.
  Documents themselves are untouched; only the baseline is rebuilt.
- **Producer**: new documents are stamped `by proj-doc-builder v4.0`. Markers
  written by 3.x still parse (only the timestamp is read), so old documents need no
  rewrite.

### Changed

- Package name: `@walvka-code/sync-project-docs` → `@walvka-code/proj-doc-builder`.
- Repository renamed: `walvka-code/sync-project-docs` → `walvka-code/proj-doc-builder`.
  The old GitHub URLs redirect; the old Pages address
  (`https://walvka-code.github.io/sync-project-docs/`) no longer serves the index.
- Repository layout: `package.json`, `cordis.patch.yml`, `index.js`, `locale/`,
  `skills/` and `scripts/` sit at the root, so `dsh.bundle` is declared by the root
  manifest. The market index drops its `source.path`.
- Market index: `https://walvka-code.github.io/proj-doc-builder/index.json`.

### Added

- `CHANGELOG.md`.
- `node:test` coverage for the bundled provider (`test/index.test.mjs`).
- `scripts/release.mjs`: repoints `index.json` at the current package commit,
  commits, pushes and prints the install spec.
- `assets/icon-180.png` for the awesome-dsh-plugin storefront tile.

## 3.2.0 — 2026-11-04

First public release, published as `sync-project-docs`: scan a repository, write
one Markdown outline per source directory, publish a hash manifest and CLAUDE
document tree, and serve the result through a verify-then-load reader.

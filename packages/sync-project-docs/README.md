# @walvka-code/sync-project-docs

DeepSeek Harness **bundle** for the `sync-project-docs` skill: scan, validate and publish
AI-readable project documentation with a committed scan state.

## What it mounts

One row in `cordis.patch.yml` registers a bundled skill provider (`rank 600`) that serves the
`sync-project-docs` skill from `skills/sync-project-docs/`. The skill ships its Python tools
(`tools/`), references and tests, and resolves every path against the skill directory the host
reports — no project-local copy is required. A repository that keeps its own
`.dsh/skills/sync-project-docs/` still wins over the bundled copy.

`index.js` reads `SKILL.md`, parses its frontmatter and exposes the body through `ctx.skills`; it has
no runtime dependency beyond Node's standard library.

## Install

```bash
# market index (Discover → add source)
https://walvka-code.github.io/sync-project-docs/index.json
# fallback: https://raw.githubusercontent.com/walvka-code/sync-project-docs/main/index.json

# pin one commit
git+https://github.com/walvka-code/sync-project-docs.git#<40-hex-commit>&path:/packages/sync-project-docs

# local checkout, via the plugin_manager tool
{ "action": "install_bundle", "target": "<checkout>/packages/sync-project-docs" }
```

## Validate without a Harness

```bash
node scripts/check-bundle.mjs
```

It checks the manifest, the patch, the packaged files, exercises the provider (list/get against the
real `SKILL.md`) and keeps `index.json` consistent with `package.json`.

## License

MIT

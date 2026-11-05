# @walvka-code/proj-doc-builder

DeepSeek Harness **bundle** for the `proj-doc-builder` skill: scan, validate and publish
AI-readable project documentation with a committed scan state.

## What it mounts

One row in `cordis.patch.yml` registers a bundled skill provider (`rank 600`) that serves the
`proj-doc-builder` skill from `skills/proj-doc-builder/`. The skill ships its Python tools
(`tools/`), references and tests, and resolves every path against the skill directory the host
reports — no project-local copy is required. A repository that keeps its own
`.dsh/skills/proj-doc-builder/` still wins over the bundled copy.

`index.js` reads `SKILL.md`, parses its frontmatter and exposes the body through `ctx.skills`; it has
no runtime dependency beyond Node's standard library.

## Install

```bash
# market index (Discover → add source)
https://walvka-code.github.io/proj-doc-builder/index.json
# fallback: https://raw.githubusercontent.com/walvka-code/proj-doc-builder/main/index.json

# pin one commit
git+https://github.com/walvka-code/proj-doc-builder.git#<40-hex-commit>&path:/packages/proj-doc-builder

# local checkout, via the plugin_manager tool
{ "action": "install_bundle", "target": "<checkout>/packages/proj-doc-builder" }
```

## Validate without a Harness

```bash
node scripts/check-bundle.mjs
```

It checks the manifest, the patch, the packaged files, exercises the provider (list/get against the
real `SKILL.md`) and keeps `index.json` consistent with `package.json`.

## License

MIT

/**
 * Offline checks for the proj-doc-builder bundle package.
 *
 * Run from the repository root:  node packages/proj-doc-builder/scripts/check-bundle.mjs
 *
 * It verifies the manifest the loader reads, exercises the bundled skill
 * provider without a Harness, and keeps the market index consistent with the
 * package it describes. No network access and no DSH installation required.
 */
import assert from "node:assert/strict";
import { readdir, readFile, stat } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const PKG_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = resolve(PKG_DIR, "..", "..");
const importFile = (file) => import(pathToFileURL(file).href);

const failures = [];
const check = async (label, fn) => {
  try {
    await fn();
    console.log(`ok   ${label}`);
  } catch (error) {
    failures.push(`${label}: ${error.message}`);
    console.log(`FAIL ${label}: ${error.message}`);
  }
};

const pkg = JSON.parse(await readFile(join(PKG_DIR, "package.json"), "utf8"));

await check("package.json declares an installable bundle", async () => {
  assert.match(pkg.name, /^(@[a-z0-9-]+\/)?[a-z0-9-]+$/);
  assert.match(pkg.version, /^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/);
  assert.equal(pkg.type, "module");
  assert.ok(pkg.dsh?.bundle?.patch, "missing dsh.bundle.patch");
  assert.equal(pkg.license, "MIT");
  assert.ok(pkg.engines?.node, "missing engines.node");
  assert.ok(pkg.engines?.dsh, "missing engines.dsh");
});

await check("bundle patch mounts this package", async () => {
  const patchPath = join(PKG_DIR, pkg.dsh.bundle.patch);
  const patch = await readFile(patchPath, "utf8");
  assert.match(patch, /- insert:/, "patch has no insert list");
  assert.ok(patch.includes(`"${pkg.name}"`) || patch.includes(`'${pkg.name}'`), "patch does not insert this package");
  assert.match(patch, /id: [a-z0-9-]+/);
});

await check("package files exist", async () => {
  for (const rel of [pkg.main, pkg.icon, "locale/en.json", "locale/zh.json"]) {
    await stat(join(PKG_DIR, rel));
  }
  const skillDir = join(PKG_DIR, "skills", "proj-doc-builder");
  for (const rel of ["SKILL.md", "tools/sync_project_docs.py", "tools/load_docs.py", "references/templates.md", "tests/conftest.py"]) {
    await stat(join(skillDir, rel));
  }
});

await check("provider registers and serves the skill", async () => {
  const module = await importFile(join(PKG_DIR, "index.js"));
  assert.equal(module.name, "proj-doc-builder");
  assert.deepEqual(module.inject, ["skills"]);
  const registered = [];
  module.apply({ skills: { registerProvider: (factory) => registered.push(factory) } });
  assert.equal(registered.length, 1);
  const provider = registered[0]();
  const list = await provider.list({});
  assert.equal(list.length, 1);
  const candidate = list[0];
  assert.equal(candidate.name, "proj-doc-builder");
  assert.equal(candidate.rank, 600);
  assert.equal(candidate.source, "bundled");
  assert.ok(candidate.description.length > 40, "description is too short to route on");
  assert.equal(candidate.invocation.modelInvocable, true);
  assert.equal(candidate.resourceBase.kind, "directory");
  await stat(candidate.resourceBase.path);
  const loaded = await provider.get(candidate, {});
  assert.equal(loaded.name, "proj-doc-builder");
  assert.ok(loaded.content.length > 5000, "SKILL.md body is suspiciously small");
  assert.ok(!loaded.content.startsWith("---"), "frontmatter leaked into the body");
});

await check("SKILL.md has no project-local install path", async () => {
  const skillDir = join(PKG_DIR, "skills", "proj-doc-builder");
  const files = [
    join(skillDir, "SKILL.md"),
    ...(await readdir(join(skillDir, "references")))
      .filter((name) => name.endsWith(".md"))
      .map((name) => join(skillDir, "references", name)),
  ];
  for (const file of files) {
    const text = await readFile(file, "utf8");
    assert.ok(!text.includes(".dsh/skills/proj-doc-builder/tools"), `${file} still hardcodes a project-local tools path`);
  }
});

await check("market index describes this package", async () => {
  const indexPath = join(REPO_ROOT, "index.json");
  let raw;
  try {
    raw = await readFile(indexPath, "utf8");
  } catch {
    console.log("      (no index.json yet — skipped)");
    return;
  }
  const index = JSON.parse(raw);
  assert.equal(index.format, 1);
  assert.equal(typeof index.title?.en, "string");
  const entry = index.plugins.find((plugin) => plugin.name === pkg.name);
  assert.ok(entry, `index.json does not list ${pkg.name}`);
  const version = entry.versions[0];
  assert.equal(version.version, pkg.version, "index version differs from package.json");
  assert.ok(Number.isInteger(version.publishTimestamp));
  if (version.source.type === "git") {
    assert.match(version.source.commit, /^[0-9a-f]{40}$/);
    assert.equal(version.source.path, "packages/proj-doc-builder");
  } else {
    assert.equal(version.source.type, "npm");
  }
  assert.ok(index.plugins.length > 0);
});

if (failures.length > 0) {
  console.error(`\n${failures.length} check(s) failed`);
  process.exit(1);
}
console.log("\nall bundle checks passed");

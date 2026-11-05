#!/usr/bin/env node
/**
 * Point the market index at the current package commit.
 *
 * The index must name a commit that contains the package, so it can only be
 * updated after the package commit exists. This script does the second commit:
 *
 *   node scripts/release.mjs --dry-run   # show what would change
 *   node scripts/release.mjs             # write and commit index.json
 *   node scripts/release.mjs --push      # write, commit and push
 *
 * It is a no-op when the index already names the current commit and version.
 */
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const INDEX = resolve(ROOT, "index.json");
const PACKAGE = resolve(ROOT, "package.json");

const argv = new Set(process.argv.slice(2));
const dryRun = argv.has("--dry-run");
const push = argv.has("--push");

const git = (...args) => execFileSync("git", args, { cwd: ROOT, encoding: "utf8" }).trim();
const tryGit = (...args) => {
  try {
    return { ok: true, out: git(...args) };
  } catch (error) {
    return { ok: false, out: String(error.stdout ?? "") + String(error.stderr ?? "") };
  }
};

const fail = (message) => {
  console.error(`release: ${message}`);
  process.exit(1);
};

if (git("status", "--porcelain") !== "") fail("working tree is not clean; commit or stash first");

const branch = git("rev-parse", "--abbrev-ref", "HEAD");
if (branch !== "main") fail(`expected branch main, found ${branch}`);

const pkg = JSON.parse(readFileSync(PACKAGE, "utf8"));
const index = JSON.parse(readFileSync(INDEX, "utf8"));
const plugin = index.plugins?.[0];
if (!plugin) fail("index.json lists no plugin");

const version = plugin.versions?.[0];
if (!version) fail("index.json lists no version");
const head = git("rev-parse", "HEAD");
const npmSource = version.source.type === "npm";
const pinned = npmSource ? null : version.source.commit;

const ancestor = pinned === null ? true : tryGit("merge-base", "--is-ancestor", pinned, head).ok;
const packageChanged =
  pinned !== null &&
  (!ancestor ||
    tryGit("diff", "--quiet", pinned, head, "--", ".", ":(exclude)index.json").ok === false);
const versionChanged = version.version !== pkg.version;

if (!packageChanged && !versionChanged) {
  const where = npmSource
    ? `npm ${plugin.name}@${version.version}`
    : `v${pkg.version} at ${pinned.slice(0, 7)}`;
  console.log(`release: up to date (${where})`);
  process.exit(0);
}

console.log(`release: v${version.version} -> v${pkg.version}`);
if (npmSource) {
  console.log(`release: source npm (${plugin.name})`);
} else {
  console.log(`release: ${pinned.slice(0, 7)} -> ${head.slice(0, 7)}`);
}
version.version = pkg.version;
if (!npmSource) version.source.commit = head;
if (versionChanged) version.publishTimestamp = Math.floor(Date.now() / 1000);

const rendered = `${JSON.stringify(index, null, 2)}\n`;
if (dryRun) {
  console.log(rendered);
  process.exit(0);
}

writeFileSync(INDEX, rendered, "utf8");
execFileSync(process.execPath, [resolve(ROOT, "scripts", "check-bundle.mjs")], {
  cwd: ROOT,
  stdio: "inherit",
});

git("add", "index.json");
git("commit", "-m", `Release v${pkg.version}`, "-m", `Market index pinned to ${head}.`);
console.log(`release: committed ${git("rev-parse", "--short", "HEAD")}`);

if (push) {
  execFileSync("git", ["push", "origin", "main"], { cwd: ROOT, stdio: "inherit" });
  console.log(`release: pushed; index https://walvka-code.github.io/proj-doc-builder/index.json`);
} else {
  console.log("release: not pushed (pass --push)");
}

const spec = npmSource
  ? `${plugin.name}@${version.version}`
  : `git+${version.source.url}.git#${version.source.commit}`;
console.log(`release: install spec ${spec}`);

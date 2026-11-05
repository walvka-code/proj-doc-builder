/**
 * Tests for the bundled provider and its frontmatter reader.
 *
 * Run with:  node --test test/
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { apply, inject, name, parseFrontMatter } from "../index.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");

/** Build a provider from a fake context, the way the Loader does. */
function makeProvider() {
  const registered = [];
  apply({ skills: { registerProvider: (factory) => registered.push(factory) } });
  assert.equal(registered.length, 1, "apply must register exactly one provider");
  return registered[0]();
}

test("module contract", () => {
  assert.equal(name, "proj-doc-builder");
  assert.deepEqual(inject, ["skills"]);
});

test("frontmatter: plain scalars and body split", () => {
  const { meta, body } = parseFrontMatter("---\nname: demo\ndescription: A skill.\n---\n\n# Title\n");
  assert.equal(meta.name, "demo");
  assert.equal(meta.description, "A skill.");
  assert.equal(body.trim(), "# Title");
});

test("frontmatter: missing block leaves the text untouched", () => {
  const text = "# No frontmatter\n\nbody\n";
  const { meta, body } = parseFrontMatter(text);
  assert.deepEqual(meta, {});
  assert.equal(body, text);
});

test("frontmatter: folded description keeps every line", () => {
  const raw = "---\nname: demo\ndescription: >\n  First line.\n  Second line.\nversion: \"1.0\"\n---\n";
  const { meta } = parseFrontMatter(raw);
  assert.equal(meta.description, "First line. Second line.");
  assert.equal(meta.version, "1.0");
});

test("frontmatter: quoted values are unquoted", () => {
  const { meta } = parseFrontMatter("---\nname: 'demo'\nversion: \"2.0\"\n---\n");
  assert.equal(meta.name, "demo");
  assert.equal(meta.version, "2.0");
});

test("frontmatter: CRLF and BOM", () => {
  const { meta, body } = parseFrontMatter("\uFEFF---\r\nname: demo\r\n---\r\n# Body\r\n");
  assert.equal(meta.name, "demo");
  assert.match(body, /^# Body/);
});

test("frontmatter: list values do not leak into metadata", () => {
  const { meta } = parseFrontMatter("---\nname: demo\nallowed-tools:\n  - Bash\n  - Read\n---\n");
  assert.equal(meta.name, "demo");
  assert.equal(meta["allowed-tools"], "");
});

test("provider lists the packaged skill", async () => {
  const provider = makeProvider();
  const candidates = await provider.list({});
  assert.equal(candidates.length, 1);
  const [candidate] = candidates;
  assert.equal(candidate.name, "proj-doc-builder");
  assert.equal(candidate.rank, 600);
  assert.equal(candidate.source, "bundled");
  assert.equal(candidate.provider, "proj-doc-builder");
  assert.equal(candidate.invocation.modelInvocable, true);
  assert.equal(candidate.invocation.userInvocable, true);
  assert.equal(candidate.resourceBase.kind, "directory");
  assert.equal(candidate.resourceBase.path, join(ROOT, "skills", "proj-doc-builder"));
  assert.ok(candidate.description.length > 40);
});

test("provider serves the body without frontmatter", async () => {
  const provider = makeProvider();
  const [candidate] = await provider.list({});
  const skill = await provider.get(candidate, {});
  assert.equal(skill.name, "proj-doc-builder");
  assert.equal(skill.resourceBase.path, join(ROOT, "skills", "proj-doc-builder"));
  assert.ok(!skill.content.startsWith("---"), "frontmatter leaked into the body");
  assert.match(skill.content, /^#\s+proj_doc_builder/m, "body should carry the skill heading");

  const onDisk = await readFile(join(ROOT, "skills", "proj-doc-builder", "SKILL.md"), "utf8");
  const expected = parseFrontMatter(onDisk).body;
  assert.equal(skill.content, expected);
});

test("provider honours an aborted signal", async () => {
  const provider = makeProvider();
  const controller = new AbortController();
  controller.abort();
  assert.deepEqual(await provider.list({ signal: controller.signal }), []);
  assert.equal(await provider.get({}, { signal: controller.signal }), undefined);
});

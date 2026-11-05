/**
 * Bundled `sync-project-docs` skill provider for DeepSeek Harness.
 *
 * This package ships the skill (SKILL.md, tools/, references/, tests/) inside
 * `skills/sync-project-docs/`. The provider reads the Markdown body at load time
 * and reports the skill directory as its resource base, so every relative path
 * in the instructions resolves beside the installed package instead of a
 * project-local `.dsh/skills/sync-project-docs/` checkout.
 *
 * Rank 600 is the bundled rank: a project-local or user-level copy of the same
 * skill still wins, which is what lets a repository pin its own version.
 *
 * @module @walvka-code/sync-project-docs
 */
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** Cordis plugin name. */
export const name = "sync-project-docs";

/** Service required by this bundled provider. */
export const inject = ["skills"];

/** Registry provider name; unique across mounted skill providers. */
const PROVIDER_NAME = "sync-project-docs";

/** Standard precedence rank for packaged skill providers and local bundled roots. */
const BUNDLED_SKILL_RANK = 600;

const SKILL_DIR = join(dirname(fileURLToPath(import.meta.url)), "skills", "sync-project-docs");
const SKILL_FILE = join(SKILL_DIR, "SKILL.md");

/** Used only when SKILL.md frontmatter cannot be read, so the catalog never loses the skill. */
const DESCRIPTION_FALLBACK =
  "Safely scan, validate, and synchronize AI-readable project documentation, the manifest and the CLAUDE document tree.";

/**
 * Strip one pair of matching quotes from a scalar.
 * @param {string} value - raw frontmatter scalar.
 * @returns {string} unquoted scalar.
 */
function unquote(value) {
  const match = /^(['"])([\s\S]*)\1$/.exec(value);
  return match ? match[2] : value;
}

/**
 * Parse the flat YAML frontmatter this skill declares.
 *
 * Only top-level `key: value` pairs and folded (`>`) or literal (`|`) block
 * scalars are modelled; nested mappings are ignored, which is enough for the
 * `name`, `description` and invocation keys the skill registry consumes.
 *
 * @param {string} text - complete SKILL.md source.
 * @returns {{ meta: Record<string, string>, body: string }} parsed metadata and the Markdown body.
 */
export function parseFrontMatter(text) {
  const source = text.replace(/^\uFEFF/, "");
  const match = /^---\r?\n([\s\S]*?)\r?\n---[ \t]*\r?\n?/.exec(source);
  if (!match) return { meta: {}, body: source };

  /** @type {Record<string, string>} */
  const meta = {};
  let blockKey = null;
  for (const line of match[1].split(/\r?\n/)) {
    if (!line.trim()) continue;
    const pair = /^([A-Za-z0-9_-]+):(.*)$/.exec(line);
    if (pair) {
      const [, key, rest] = pair;
      const value = rest.trim();
      if (value === ">" || value === "|" || value === ">-" || value === "|-") {
        blockKey = key;
        meta[key] = "";
      } else if (value === "") {
        blockKey = null;
        meta[key] = "";
      } else {
        blockKey = null;
        meta[key] = unquote(value);
      }
      continue;
    }
    if (blockKey && /^\s+\S/.test(line)) {
      const piece = line.trim();
      meta[blockKey] = meta[blockKey] ? `${meta[blockKey]} ${piece}` : piece;
    }
  }
  for (const key of Object.keys(meta)) meta[key] = meta[key].replace(/\s+/g, " ").trim();
  return { meta, body: source.slice(match[0].length) };
}

/**
 * Resolve invocation controls from frontmatter flags.
 * @param {Record<string, string>} meta - parsed frontmatter.
 * @returns {{ modelInvocable: boolean, userInvocable: boolean }} invocation policy.
 */
function invocationOf(meta) {
  const flag = (key, fallback) => {
    const value = meta[key];
    if (value === undefined || value === "") return fallback;
    return value === "true";
  };
  return {
    modelInvocable: !flag("disable-model-invocation", false),
    userInvocable: flag("user-invocable", true),
  };
}

/** @type {{ meta: Record<string, string>, body: string } | undefined} */
let cachedSkill;

/**
 * Read and cache SKILL.md.
 * @param {{ signal?: AbortSignal }} [options] - lookup options.
 * @returns {Promise<{ meta: Record<string, string>, body: string }>} parsed skill.
 */
async function loadSkill(options) {
  options?.signal?.throwIfAborted?.();
  if (!cachedSkill) {
    cachedSkill = parseFrontMatter(await readFile(SKILL_FILE, "utf8"));
  }
  return cachedSkill;
}

/**
 * Build the registry-facing summary of one parsed skill.
 * @param {Record<string, string>} meta - parsed frontmatter.
 * @returns {object} skill candidate without `rank`/`locator`.
 */
function summaryOf(meta) {
  return {
    name: meta.name || PROVIDER_NAME,
    description: meta.description || DESCRIPTION_FALLBACK,
    invocation: invocationOf(meta),
    source: "bundled",
    provider: PROVIDER_NAME,
    resourceBase: { kind: "directory", path: SKILL_DIR },
  };
}

/** @type {import('@deepseek-ai/dsh-skill').SkillProvider} */
const provider = {
  name: PROVIDER_NAME,
  async list(options = {}) {
    if (options.signal?.aborted) return [];
    const { meta } = await loadSkill(options);
    return [{ ...summaryOf(meta), rank: BUNDLED_SKILL_RANK, locator: SKILL_FILE }];
  },
  async get(_candidate, options = {}) {
    if (options.signal?.aborted) return undefined;
    const { meta, body } = await loadSkill(options);
    return { ...summaryOf(meta), content: body };
  },
};

/**
 * Register the bundled provider on `ctx.skills`.
 * @param {import('@deepseek-ai/cordis').Context} ctx - owning context.
 * @returns {void}
 */
export function apply(ctx) {
  ctx.skills.registerProvider(() => provider);
}

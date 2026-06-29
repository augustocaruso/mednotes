#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const SELF_CHECK_SCHEMA = "medical-notes-workbench.markdown-db-self-check.v1";
const STATUS_SCHEMA = "medical-notes-workbench.markdown-db-status.v1";
const REBUILD_SCHEMA = "medical-notes-workbench.markdown-db-rebuild.v1";
const PROBE_SCHEMA = "medical-notes-workbench.markdown-db-probe.v1";
const CHAT_METADATA_SCHEMA = "medical-notes-workbench.chat-metadata.v1";
const ERROR_SCHEMA = "medical-notes-workbench.markdown-db-error.v1";
const CACHE_MANIFEST_SCHEMA = "medical-notes-workbench.markdown-db-cache-manifest.v1";
const BLOCKED_REASON = "markdown_query_index_unavailable";
const NEXT_ACTION = "Rodar /mednotes:setup para preparar o índice Markdown e repetir o workflow.";
const NODE_PATH_ENV = "MEDNOTES_MARKDOWNDB_NODE_PATH";

class MarkdownDbAdapterError extends Error {
  constructor(message, details = {}) {
    super(message);
    this.name = "MarkdownDbAdapterError";
    this.details = details;
  }
}

function parseArgs(argv) {
  const command = argv[0];
  const options = {};
  const rest = argv.slice(1);
  for (let index = 0; index < rest.length; index += 1) {
    const arg = rest[index];
    if (!arg.startsWith("--")) {
      throw new MarkdownDbAdapterError(`Unexpected argument: ${arg}`);
    }
    const key = arg.slice(2).replaceAll("-", "_");
    const value = rest[index + 1];
    if (!value || value.startsWith("--")) {
      throw new MarkdownDbAdapterError(`Missing value for ${arg}`);
    }
    options[key] = value;
    index += 1;
  }
  return { command, options };
}

function requireOption(options, key) {
  const value = options[key];
  if (!value) {
    throw new MarkdownDbAdapterError(`Missing --${key.replaceAll("_", "-")}`);
  }
  return path.resolve(value);
}

function writeJson(stream, payload) {
  stream.write(`${JSON.stringify(payload, null, 2)}\n`);
}

function toPosix(value) {
  return value.split(path.sep).join("/");
}

async function exists(target) {
  try {
    await fs.access(target);
    return true;
  } catch {
    return false;
  }
}

async function readJson(target) {
  try {
    return JSON.parse(await fs.readFile(target, "utf8"));
  } catch {
    return null;
  }
}

async function sha256File(target) {
  return crypto
    .createHash("sha256")
    .update(await fs.readFile(target))
    .digest("hex");
}

async function loadMarkdownDb() {
  const nodePath = process.env[NODE_PATH_ENV];
  if (nodePath) {
    const packageEntry = path.resolve(nodePath, "mddb", "dist", "src", "index.js");
    if (!(await exists(packageEntry))) {
      throw new MarkdownDbAdapterError("MarkdownDB package was not found in the configured Node runtime.", {
        node_modules_path: nodePath,
        expected_entry: packageEntry,
      });
    }
    return import(pathToFileURL(packageEntry).href);
  }
  try {
    return await import("mddb");
  } catch (error) {
    throw new MarkdownDbAdapterError("MarkdownDB package could not be imported.", {
      package: "mddb",
      cause: String(error?.message || error),
    });
  }
}

async function listMarkdownFiles(root) {
  if (!(await exists(root))) {
    throw new MarkdownDbAdapterError("Markdown source directory does not exist.", { path: root });
  }
  const files = [];
  async function walk(dir) {
    const entries = await fs.readdir(dir, { withFileTypes: true });
    for (const entry of entries) {
      const entryPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        await walk(entryPath);
      } else if (entry.isFile() && entry.name.toLowerCase().endsWith(".md")) {
        files.push(entryPath);
      }
    }
  }
  await walk(root);
  return files.sort((left, right) => left.localeCompare(right));
}

async function sourceFingerprint({ wikiDir, rawDir }) {
  const roots = [
    { role: "raw", root: rawDir },
    { role: "wiki", root: wikiDir },
  ];
  const entries = [];
  for (const { role, root } of roots) {
    const files = await listMarkdownFiles(root);
    for (const file of files) {
      const stat = await fs.stat(file);
      entries.push({
        role,
        root,
        relative_path: toPosix(path.relative(root, file)),
        size: stat.size,
        mtime_ms: Math.round(stat.mtimeMs),
        sha256: await sha256File(file),
      });
    }
  }
  const digest = crypto.createHash("sha256").update(JSON.stringify(entries)).digest("hex");
  return { digest, entries };
}

function cachePaths(cacheDir) {
  return {
    cacheDir,
    sourceRoot: path.join(cacheDir, "source"),
    rawMirror: path.join(cacheDir, "source", "Chats_Raw"),
    wikiMirror: path.join(cacheDir, "source", "Wiki_Medicina"),
    dbPath: path.join(cacheDir, "markdown-db.sqlite"),
    manifestPath: path.join(cacheDir, "manifest.json"),
  };
}

async function copyMarkdownTree(sourceRoot, targetRoot) {
  const files = await listMarkdownFiles(sourceRoot);
  for (const source of files) {
    const relative = path.relative(sourceRoot, source);
    const target = path.join(targetRoot, relative);
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.copyFile(source, target);
  }
  return files.length;
}

async function currentStatus({ wikiDir, rawDir, cacheDir }) {
  const paths = cachePaths(cacheDir);
  const fingerprint = await sourceFingerprint({ wikiDir, rawDir });
  const manifest = await readJson(paths.manifestPath);
  const dbExists = await exists(paths.dbPath);
  if (!manifest || !dbExists) {
    return {
      schema: STATUS_SCHEMA,
      status: "missing",
      stale_reason: "cache_missing",
      cache_dir: cacheDir,
      source_fingerprint: fingerprint.digest,
    };
  }
  if (manifest.source_fingerprint !== fingerprint.digest) {
    return {
      schema: STATUS_SCHEMA,
      status: "stale",
      stale_reason: "source_fingerprint_changed",
      cache_dir: cacheDir,
      source_fingerprint: fingerprint.digest,
      cached_source_fingerprint: manifest.source_fingerprint || "",
    };
  }
  return {
    schema: STATUS_SCHEMA,
    status: "ready",
    cache_dir: cacheDir,
    source_fingerprint: fingerprint.digest,
    raw_count: manifest.raw_count,
    wiki_count: manifest.wiki_count,
  };
}

async function withMarkdownDb(cacheDir, callback) {
  const { MarkdownDB } = await loadMarkdownDb();
  const paths = cachePaths(cacheDir);
  const db = new MarkdownDB({
    client: "sqlite3",
    connection: { filename: paths.dbPath },
    useNullAsDefault: true,
  });
  await db.init();
  try {
    return await callback(db, paths);
  } finally {
    if (db.db) {
      await db.db.destroy();
    }
  }
}

async function rebuildCache({ wikiDir, rawDir, cacheDir }) {
  const started = Date.now();
  const paths = cachePaths(cacheDir);
  const fingerprint = await sourceFingerprint({ wikiDir, rawDir });
  await fs.mkdir(cacheDir, { recursive: true });
  await fs.rm(paths.sourceRoot, { recursive: true, force: true });
  await fs.rm(paths.dbPath, { force: true });
  await fs.rm(path.join(cacheDir, ".markdowndb"), { recursive: true, force: true });
  const rawCount = await copyMarkdownTree(rawDir, paths.rawMirror);
  const wikiCount = await copyMarkdownTree(wikiDir, paths.wikiMirror);

  await withMarkdownDb(cacheDir, async (db, cache) => {
    const previousCwd = process.cwd();
    try {
      process.chdir(cache.cacheDir);
      await db.indexFolder({
        folderPath: cache.sourceRoot,
        customConfig: { include: ["**/*.md"] },
      });
    } finally {
      process.chdir(previousCwd);
    }
  });

  const manifest = {
    schema: CACHE_MANIFEST_SCHEMA,
    status: "ready",
    source_fingerprint: fingerprint.digest,
    source_entries: fingerprint.entries,
    raw_count: rawCount,
    wiki_count: wikiCount,
    cache_dir: cacheDir,
    source_root: paths.sourceRoot,
    db_path: paths.dbPath,
    rebuilt_at: new Date().toISOString(),
  };
  await fs.writeFile(paths.manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  return {
    schema: REBUILD_SCHEMA,
    status: "ready",
    cache: { status: "rebuilt", cache_dir: cacheDir },
    raw_count: rawCount,
    wiki_count: wikiCount,
    source_fingerprint: fingerprint.digest,
    timing_ms: { total: Date.now() - started },
  };
}

async function ensureCurrentCache({ wikiDir, rawDir, cacheDir }) {
  const status = await currentStatus({ wikiDir, rawDir, cacheDir });
  if (status.status === "ready") {
    return { cacheStatus: "current", status };
  }
  const rebuilt = await rebuildCache({ wikiDir, rawDir, cacheDir });
  return { cacheStatus: "rebuilt", status: rebuilt };
}

function normalizeValue(value) {
  if (value instanceof Date) {
    return value.toISOString().replace(".000Z", "Z");
  }
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$/.test(value)) {
    return value.replace(".000Z", "Z");
  }
  if (Array.isArray(value)) {
    return value.map((item) => normalizeValue(item));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, nested]) => [key, normalizeValue(nested)]));
  }
  return value;
}

function metadataOf(file) {
  return normalizeValue(file.metadata || {});
}

function rawChatFromFile(file) {
  const metadata = metadataOf(file);
  const id = metadata.fonte_id || metadata.chat_id || "";
  return {
    id,
    title: metadata.titulo_triagem || metadata.title || "",
    url: id ? `https://gemini.google.com/app/${id}` : "",
    date_created: metadata.date_created || "",
    date_exported: metadata.exported_at || metadata.date_exported || "",
    raw_path: file.url_path || "",
  };
}

function sortByUrlPath(files) {
  return [...files].sort((left, right) => String(left.url_path || "").localeCompare(String(right.url_path || "")));
}

function sortChatIdsByDate(rawFiles) {
  return rawFiles
    .map(rawChatFromFile)
    .filter((chat) => chat.id)
    .sort((left, right) => {
      const leftDate = left.date_created || "9999-12-31T23:59:59Z";
      const rightDate = right.date_created || "9999-12-31T23:59:59Z";
      return leftDate.localeCompare(rightDate) || left.id.localeCompare(right.id);
    })
    .map((chat) => chat.id);
}

async function indexedFiles(cacheDir) {
  return withMarkdownDb(cacheDir, async (db) => db.getFiles({ extensions: ["md"] }));
}

function splitIndexedFiles(files) {
  const rawFiles = [];
  const wikiFiles = [];
  for (const file of files) {
    const urlPath = String(file.url_path || "");
    if (urlPath.startsWith("Chats_Raw/")) {
      rawFiles.push(file);
    } else if (urlPath.startsWith("Wiki_Medicina/")) {
      wikiFiles.push(file);
    }
  }
  return { rawFiles: sortByUrlPath(rawFiles), wikiFiles: sortByUrlPath(wikiFiles) };
}

async function probe({ wikiDir, rawDir, cacheDir }) {
  const started = Date.now();
  const { cacheStatus } = await ensureCurrentCache({ wikiDir, rawDir, cacheDir });
  const files = await indexedFiles(cacheDir);
  const { rawFiles, wikiFiles } = splitIndexedFiles(files);
  const selectedRaw = rawFiles.find((file) => metadataOf(file).fonte_id === "abc123") || rawFiles[0] || null;
  const selectedWiki = wikiFiles[0] || null;
  return {
    schema: PROBE_SCHEMA,
    status: "ready",
    raw_count: rawFiles.length,
    wiki_count: wikiFiles.length,
    cache: { status: cacheStatus, cache_dir: cacheDir },
    chat: selectedRaw ? rawChatFromFile(selectedRaw) : null,
    wiki_frontmatter: selectedWiki ? metadataOf(selectedWiki) : {},
    date_ordered_chat_ids: sortChatIdsByDate(rawFiles),
    timing_ms: { total: Date.now() - started },
  };
}

async function lookupChat({ wikiDir, rawDir, cacheDir, chatId }) {
  await ensureCurrentCache({ wikiDir, rawDir, cacheDir });
  const files = await indexedFiles(cacheDir);
  const { rawFiles } = splitIndexedFiles(files);
  const match = rawFiles.find((file) => {
    const metadata = metadataOf(file);
    return metadata.fonte_id === chatId || metadata.chat_id === chatId;
  });
  return {
    schema: CHAT_METADATA_SCHEMA,
    status: match ? "ready" : "missing",
    chat: match ? rawChatFromFile(match) : null,
  };
}

async function main() {
  const { command, options } = parseArgs(process.argv.slice(2));
  if (command === "self-check") {
    await loadMarkdownDb();
    return {
      schema: SELF_CHECK_SCHEMA,
      status: "ready",
      package: "mddb",
      module_resolved: true,
    };
  }

  const wikiDir = requireOption(options, "wiki_dir");
  const rawDir = requireOption(options, "raw_dir");
  const cacheDir = requireOption(options, "cache_dir");
  if (command === "status") {
    return currentStatus({ wikiDir, rawDir, cacheDir });
  }
  if (command === "rebuild") {
    return rebuildCache({ wikiDir, rawDir, cacheDir });
  }
  if (command === "probe") {
    return probe({ wikiDir, rawDir, cacheDir });
  }
  if (command === "lookup-chat") {
    if (!options.chat_id) {
      throw new MarkdownDbAdapterError("Missing --chat-id");
    }
    return lookupChat({ wikiDir, rawDir, cacheDir, chatId: options.chat_id });
  }
  throw new MarkdownDbAdapterError(`Unknown command: ${command || ""}`);
}

main()
  .then((payload) => {
    writeJson(process.stdout, payload);
  })
  .catch((error) => {
    const details =
      error instanceof MarkdownDbAdapterError ? error.details : { cause: String(error?.message || error) };
    writeJson(process.stderr, {
      schema: ERROR_SCHEMA,
      status: "blocked",
      blocked_reason: BLOCKED_REASON,
      next_action: NEXT_ACTION,
      error: String(error?.message || error),
      details,
    });
    process.exitCode = 1;
  });

#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const PLUGIN_NAME = "marketplaces";
const MARKETPLACE_NAME = "kirill-local";
const __filename = fileURLToPath(import.meta.url);
const packageRoot = path.resolve(path.dirname(__filename), "..");
const packageJson = JSON.parse(
  fs.readFileSync(path.join(packageRoot, "package.json"), "utf8"),
);
const version = packageJson.version || "0.1.0";
const home = os.homedir();
const sourceRoot = path.join(home, "plugins", PLUGIN_NAME);
const cacheRoot = path.join(
  home,
  ".codex",
  "plugins",
  "cache",
  MARKETPLACE_NAME,
  PLUGIN_NAME,
  version,
);
const agentsMarketplace = path.join(home, ".agents", "plugins", "marketplace.json");
const codexConfig = path.join(home, ".codex", "config.toml");
const browserProfile = path.join(home, ".codex", `${PLUGIN_NAME}-browser-profile`);

const args = new Set(process.argv.slice(2));
const skipLogin = args.has("--skip-login");
const skipPython = args.has("--skip-python");

function log(message) {
  process.stdout.write(`[marketplaces] ${message}\n`);
}

function run(cmd, cmdArgs, options = {}) {
  const result = spawnSync(cmd, cmdArgs, {
    stdio: "inherit",
    shell: false,
    ...options,
  });
  if (result.status !== 0) {
    throw new Error(`${cmd} ${cmdArgs.join(" ")} failed`);
  }
}

function runCapture(cmd, cmdArgs) {
  return spawnSync(cmd, cmdArgs, {
    stdio: ["ignore", "pipe", "ignore"],
    encoding: "utf8",
    shell: false,
  });
}

function samePath(a, b) {
  try {
    return fs.realpathSync(a) === fs.realpathSync(b);
  } catch {
    return path.resolve(a) === path.resolve(b);
  }
}

function shouldSkip(rel) {
  const parts = rel.split(path.sep);
  const base = parts[parts.length - 1];
  if (!rel) return false;
  if (parts.includes(".git") || parts.includes("node_modules")) return true;
  if (parts.includes("__pycache__") || base.endsWith(".pyc")) return true;
  if (parts.includes(".venv") || rel.startsWith(`data${path.sep}.venv`)) return true;
  if (base === ".DS_Store") return true;
  if (base === "wb_last_search.json") return true;
  if (rel.startsWith(`data${path.sep}`) && base.endsWith(".json")) return true;
  if (base === "result.png") return true;
  if (base.endsWith(".html") || base.startsWith("product_")) return true;
  return false;
}

function copyDir(src, dest, root = src) {
  const rel = path.relative(root, src);
  if (shouldSkip(rel)) return;
  const stat = fs.statSync(src);
  if (stat.isDirectory()) {
    fs.mkdirSync(dest, { recursive: true });
    for (const entry of fs.readdirSync(src)) {
      copyDir(path.join(src, entry), path.join(dest, entry), root);
    }
    return;
  }
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.copyFileSync(src, dest);
  fs.chmodSync(dest, stat.mode);
}

function copyBundle(dest) {
  if (samePath(packageRoot, dest)) {
    log(`source already at ${dest}`);
    return;
  }
  if (!fs.existsSync(path.join(dest, ".git"))) {
    fs.rmSync(dest, { recursive: true, force: true });
  } else {
    log(`preserving git checkout at ${dest}`);
  }
  copyDir(packageRoot, dest);
  fs.mkdirSync(path.join(dest, "data"), { recursive: true });
}

function findPython() {
  const candidates = [
    process.env.PYTHON ? [process.env.PYTHON, []] : null,
    ["/opt/homebrew/opt/python@3.12/bin/python3.12", []],
    ["python3.12", []],
    ["python3", []],
    ["python", []],
    ["py", ["-3"]],
  ].filter(Boolean);
  for (const [cmd, prefixArgs] of candidates) {
    const result = runCapture(cmd, [
      ...prefixArgs,
      "-c",
      "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)",
    ]);
    if (result.status === 0) return { cmd, prefixArgs };
  }
  throw new Error("Python 3.10+ was not found. Install Python and rerun.");
}

function venvPython(root) {
  const exe = process.platform === "win32" ? "python.exe" : "python";
  return path.join(root, ".venv", process.platform === "win32" ? "Scripts" : "bin", exe);
}

function ensureVenv(root) {
  const py = venvPython(root);
  if (skipPython && fs.existsSync(py)) return py;
  const basePython = findPython();
  if (!fs.existsSync(py)) {
    log(`creating Python venv with ${basePython.cmd}`);
    run(basePython.cmd, [...basePython.prefixArgs, "-m", "venv", path.join(root, ".venv")]);
  }
  if (!skipPython) {
    log("installing Python dependencies");
    run(py, ["-m", "pip", "install", "--upgrade", "pip"]);
    run(py, ["-m", "pip", "install", "-r", path.join(root, "requirements.txt")]);
  }
  return py;
}

function writeMcp(root, pythonPath) {
  const payload = {
    mcpServers: {
      [PLUGIN_NAME]: {
        command: pythonPath,
        args: ["./scripts/marketplace_products_server.py"],
        cwd: ".",
      },
    },
  };
  fs.writeFileSync(
    path.join(root, ".mcp.json"),
    `${JSON.stringify(payload, null, 2)}\n`,
    "utf8",
  );
}

function updateMarketplaceJson() {
  fs.mkdirSync(path.dirname(agentsMarketplace), { recursive: true });
  let data = {
    name: MARKETPLACE_NAME,
    interface: { displayName: "Kirill Local Plugins" },
    plugins: [],
  };
  if (fs.existsSync(agentsMarketplace)) {
    data = JSON.parse(fs.readFileSync(agentsMarketplace, "utf8"));
    data.plugins ||= [];
    data.interface ||= { displayName: "Kirill Local Plugins" };
  }
  const entry = {
    name: PLUGIN_NAME,
    source: {
      source: "local",
      path: `./plugins/${PLUGIN_NAME}`,
    },
    policy: {
      installation: "AVAILABLE",
      authentication: "ON_INSTALL",
    },
    category: "Productivity",
  };
  const index = data.plugins.findIndex((item) => item.name === PLUGIN_NAME);
  if (index >= 0) data.plugins[index] = entry;
  else data.plugins.push(entry);
  data.plugins = data.plugins.filter((item) => item.name !== "marketplace-products");
  fs.writeFileSync(agentsMarketplace, `${JSON.stringify(data, null, 2)}\n`, "utf8");
}

function upsertTomlBlock(text, header, bodyLines) {
  const escaped = header.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const block = `${header}\n${bodyLines.join("\n")}\n`;
  const pattern = new RegExp(`${escaped}\\n[\\s\\S]*?(?=\\n\\[[^\\n]+\\]|$)`);
  if (pattern.test(text)) return text.replace(pattern, block.trimEnd());
  return `${text.trimEnd()}\n\n${block}`.trimStart();
}

function updateCodexConfig() {
  fs.mkdirSync(path.dirname(codexConfig), { recursive: true });
  let text = fs.existsSync(codexConfig) ? fs.readFileSync(codexConfig, "utf8") : "";
  const now = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  text = upsertTomlBlock(text, `[marketplaces.${MARKETPLACE_NAME}]`, [
    `last_updated = "${now}"`,
    `source_type = "local"`,
    `source = ${JSON.stringify(home)}`,
  ]);
  text = upsertTomlBlock(text, `[plugins."${PLUGIN_NAME}@${MARKETPLACE_NAME}"]`, [
    "enabled = true",
  ]);
  if (text.includes(`[plugins."marketplace-products@${MARKETPLACE_NAME}"]`)) {
    text = upsertTomlBlock(
      text,
      `[plugins."marketplace-products@${MARKETPLACE_NAME}"]`,
      ["enabled = false"],
    );
  }
  fs.writeFileSync(codexConfig, `${text.trimEnd()}\n`, "utf8");
}

function runLogin(pythonPath) {
  if (skipLogin) {
    log("browser login skipped");
    return;
  }
  log("opening Ozon and Wildberries login windows");
  run(pythonPath, [
    path.join(sourceRoot, "scripts", "browser_login.py"),
    "--source",
    "all",
    "--source-data",
    path.join(sourceRoot, "data"),
    "--cache-data",
    path.join(cacheRoot, "data"),
    "--profile",
    browserProfile,
  ]);
}

function main() {
  log(`installing ${PLUGIN_NAME}@${version}`);
  copyBundle(sourceRoot);
  copyBundle(cacheRoot);
  const pythonPath = ensureVenv(sourceRoot);
  writeMcp(cacheRoot, pythonPath);
  updateMarketplaceJson();
  updateCodexConfig();
  runLogin(pythonPath);
  log(`installed as ${PLUGIN_NAME}@${MARKETPLACE_NAME}`);
  log("restart Codex if this chat does not show the new MCP tools yet");
}

try {
  main();
} catch (error) {
  process.stderr.write(`[marketplaces] install failed: ${error.message}\n`);
  process.exit(1);
}

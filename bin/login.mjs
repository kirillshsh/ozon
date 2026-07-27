#!/usr/bin/env node

import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const installScript = path.join(path.dirname(__filename), "install.mjs");
const result = spawnSync(
  process.execPath,
  [installScript, "--login-only", ...process.argv.slice(2)],
  { stdio: "inherit", shell: false },
);

process.exit(result.status ?? 1);

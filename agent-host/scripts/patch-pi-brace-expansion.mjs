import {
  cpSync,
  existsSync,
  readFileSync,
  realpathSync,
  rmSync,
} from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Pi 0.83.0 ships an npm-shrinkwrap that pins brace-expansion 5.0.7, so npm
// overrides cannot replace GHSA-mh99-v99m-4gvg. Keep the upstream lock intact,
// install 5.0.9 at the Host root, then overlay only Pi's exact nested package.
const hostRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const safePackage = join(hostRoot, "node_modules", "brace-expansion");
const piPackage = join(
  hostRoot,
  "node_modules",
  "@earendil-works",
  "pi-coding-agent",
);
const targetPackage = join(piPackage, "node_modules", "brace-expansion");
const checkOnly = process.argv.includes("--check");

assertNested(hostRoot, safePackage);
assertNested(hostRoot, piPackage);
assertNested(piPackage, targetPackage);
if (!existsSync(safePackage) || !existsSync(piPackage)) {
  throw new Error("Pi or the safe brace-expansion package is not installed");
}

const safeVersion = packageVersion(safePackage);
if (!isSafeVersion(safeVersion)) {
  throw new Error(`Expected brace-expansion >=5.0.8, found ${safeVersion}`);
}

const installedVersion = existsSync(targetPackage)
  ? packageVersion(targetPackage)
  : "missing";
if (checkOnly) {
  if (installedVersion !== safeVersion) {
    throw new Error(
      `Pi brace-expansion patch is missing: expected ${safeVersion}, found ${installedVersion}`,
    );
  }
  console.log(`Pi brace-expansion runtime is patched to ${installedVersion}`);
  process.exit(0);
}

if (installedVersion !== safeVersion) {
  rmSync(targetPackage, { recursive: true, force: true });
  cpSync(safePackage, targetPackage, { recursive: true });
}
console.log(`Pi brace-expansion runtime patched to ${safeVersion}`);

function packageVersion(directory) {
  return JSON.parse(readFileSync(join(directory, "package.json"), "utf8")).version;
}

function isSafeVersion(version) {
  const [major, minor, patch] = version.split(".").map(Number);
  return major > 5 || (major === 5 && (minor > 0 || patch >= 8));
}

function assertNested(parent, child) {
  const canonicalParent = existsSync(parent) ? realpathSync(parent) : resolve(parent);
  const canonicalChild = existsSync(child) ? realpathSync(child) : resolve(child);
  const path = relative(canonicalParent, canonicalChild);
  if (
    !path ||
    path === ".." ||
    path.startsWith(`..${process.platform === "win32" ? "\\" : "/"}`)
  ) {
    throw new Error("Refusing to patch a dependency outside Agent Host");
  }
}

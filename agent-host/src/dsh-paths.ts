import { join } from "node:path";

export type DshSessionCompression = "none" | "zstd";

/** Encode one dsh session-id segment without importing dsh packages. */
export function dshEncodeSegment(raw: string): string {
  if (raw.length === 0) throw new Error("cannot encode an empty path segment");
  if (raw === ".") return "~002E";
  if (raw === "..") return "~002E~002E";
  let out = "";
  for (let index = 0; index < raw.length; index += 1) {
    const code = raw.charCodeAt(index);
    const ch = String.fromCharCode(code);
    if (ch !== "~" && /^[A-Za-z0-9._-]$/.test(ch)) {
      out += ch;
    } else {
      out += `~${code.toString(16).toUpperCase().padStart(4, "0")}`;
    }
  }
  return out;
}

/** Encode a workspace path into dsh's project-directory key. */
export function dshProjectKey(cwd: string): string {
  if (cwd.length === 0) {
    throw new Error("cannot encode an empty project path");
  }
  let readable = "";
  let separatorRun = false;
  for (let index = 0; index < cwd.length; index += 1) {
    const code = cwd.charCodeAt(index);
    const ch = String.fromCharCode(code);
    if (ch === "/" || ch === "\\" || ch === ":") {
      if (!separatorRun) readable += "-";
      separatorRun = true;
    } else if (ch !== "~" && /^[A-Za-z0-9._-]$/.test(ch)) {
      readable += ch;
      separatorRun = false;
    } else {
      readable += `~${code.toString(16).toUpperCase().padStart(4, "0")}`;
      separatorRun = false;
    }
  }
  const slug = readable.replace(/^-+/, "") || "root";
  return `--${slug.slice(0, 251)}--`;
}

export function dshSessionLogPath(
  sessionRoot: string,
  cwd: string,
  sessionId: string,
  compression: DshSessionCompression = "none",
): string {
  return join(
    sessionRoot,
    dshProjectKey(cwd),
    dshEncodeSegment(sessionId),
    compression === "zstd" ? "session.jsonl.zstd" : "session.jsonl",
  );
}

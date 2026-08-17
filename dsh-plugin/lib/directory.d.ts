/**
 * Session directory helpers shared by the worker (invocation upserts) and
 * the `agent-society-directory` plugin (local-session sync + mirror cache).
 *
 * The mirror (`~/.dsh/agent-society-directory.json`, 0600) is a local cache
 * of the Hub's per-principal directory: own sessions keep full depth-0/1
 * rows, other sessions keep their latest pushed row. It is derived state —
 * the Hub log remains authoritative.
 */
export interface DirectoryRow {
    readonly session_id: string;
    readonly title?: string;
    readonly workspace: string;
    readonly status: string;
    readonly last_active_at: number;
    readonly session_mode: string;
    readonly tool_policy: string;
    readonly invocations: ReadonlyArray<{
        readonly task_id?: string;
        readonly run_id?: string;
        readonly objective: string;
        readonly status: string;
        readonly at: number;
    }>;
}
export interface DirectoryMirror {
    seq: number;
    updated_at: number;
    rows: Record<string, DirectoryRow>;
}
export declare const MIRROR_MAX_INVOCATIONS = 10;
export declare function mirrorPath(dshHome: string): string;
export declare function loadMirror(path: string): DirectoryMirror;
export declare function saveMirror(path: string, mirror: DirectoryMirror): void;
/** Merge one invocation into the row for a session (bounded history). */
export declare function mergeInvocation(options: {
    row: DirectoryRow | undefined;
    sessionId: string;
    workspace: string;
    title: string | undefined;
    sessionMode: string;
    toolPolicy: string;
    invocation: DirectoryRow['invocations'][number];
}): DirectoryRow;
/** Build a depth-0/1 row for a local session from its persistence header. */
export declare function buildLocalRow(options: {
    sessionId: string;
    title: string | undefined;
    workspace: string;
    lastActiveAt: number;
    sessionMode: string;
    toolPolicy: string;
}): DirectoryRow;
/** Read session titles from the dsh projection cache when present. */
export declare function loadProjectionTitles(dshHome: string): Map<string, string>;

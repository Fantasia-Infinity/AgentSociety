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
    readonly actor_id?: string;
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
/** One consensus entry cached for prompt injection (bounded). */
export interface ConsensusEntry {
    readonly seq: number;
    readonly kind: string;
    readonly session_id?: string;
    readonly summary: string;
}
export interface DirectoryMirror {
    seq: number;
    updated_at: number;
    rows: Record<string, DirectoryRow>;
    consensus: {
        seq: number;
        entries: ConsensusEntry[];
    };
}
export declare const MIRROR_MAX_INVOCATIONS = 10;
export declare const MIRROR_MAX_CONSENSUS_ENTRIES = 24;
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
    actorId?: string;
    invocation: DirectoryRow['invocations'][number];
}): DirectoryRow;
/** Build a depth-0/1 row for a local session from its persistence header. */
export declare function buildLocalRow(options: {
    sessionId: string;
    actorId: string;
    title: string | undefined;
    workspace: string;
    lastActiveAt: number;
    sessionMode: string;
    toolPolicy: string;
}): DirectoryRow;
/** Read session titles from the dsh projection cache when present. */
export declare function loadProjectionTitles(dshHome: string): Map<string, string>;
/** Projection-cache activity: title and lastPromptAt (ms epoch) per session. */
export declare function loadProjectionActivity(dshHome: string): Map<string, {
    title?: string;
    lastPromptAt?: number;
}>;
export declare const DIRECTORY_INDEX_SECTION = "agent-society:directory-index";
export declare const CONSENSUS_SECTION = "agent-society:consensus";
export declare const PROMPT_BUDGET_CHARS = 4000;
/** One-line consensus summaries for the prompt (most recent first). */
export declare function consensusPromptLines(mirror: DirectoryMirror): string[];
/** Ranked directory index lines: working > recently active > recent rows. */
export declare function directoryPromptLines(mirror: DirectoryMirror): string[];
/**
 * Build the two bounded prompt sections for one assembly. Total text stays
 * under {@link PROMPT_BUDGET_CHARS}; entries are dropped from the directory
 * index first, then the consensus list, until the budget fits.
 */
export declare function buildSharedContextSections(mirror: DirectoryMirror): Array<{
    name: string;
    text: string;
}>;

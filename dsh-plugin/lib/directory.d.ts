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
/** Prompt-facing directory data. Volatile activity timestamps and invocation
 * history deliberately stay out of this shape so an idle mirror refresh does
 * not invalidate the shared-context message. */
export interface SharedDirectoryRow {
    readonly session_id: string;
    readonly actor_id?: string;
    readonly title?: string;
    readonly workspace: string;
    readonly status: string;
    readonly session_mode: string;
    readonly tool_policy: string;
}
/** Stable, semantic state used by the balanced shared-context renderer. */
export interface SharedContextState {
    readonly directorySeq: number;
    readonly consensusSeq: number;
    readonly consensusEntries: readonly ConsensusEntry[];
    readonly directoryRows: readonly SharedDirectoryRow[];
}
export type SharedContextUpdateKind = 'initial' | 'delta' | 'snapshot';
export interface SharedContextUpdate {
    readonly kind: SharedContextUpdateKind;
    readonly text: string;
    readonly fingerprint: string;
}
export declare const BALANCED_INITIAL_BUDGET_CHARS = 1600;
export declare const BALANCED_DELTA_BUDGET_CHARS = 800;
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
/**
 * Project a mirror into the semantic fields that may enter a model request.
 * The projection is intentionally independent of object insertion order and
 * `last_active_at`; both are cache-hostile sources of otherwise meaningless
 * prompt churn.
 */
export declare function normalizeSharedContextState(mirror: DirectoryMirror, currentSessionId?: string): SharedContextState;
/** Stable semantic fingerprint; revisions are intentionally excluded. */
export declare function sharedContextStateFingerprint(state: SharedContextState): string;
/**
 * Render one cache-friendly update. A removed row/entry produces a fresh
 * compact snapshot because a delta cannot safely prove that no other stale
 * state remains in a model's earlier transcript.
 */
export declare function renderSharedContextUpdate(previous: SharedContextState | undefined, current: SharedContextState): SharedContextUpdate | undefined;
export declare const DIRECTORY_INDEX_SECTION = "agent-society:directory-index";
export declare const CONSENSUS_SECTION = "agent-society:consensus";
export declare const PROMPT_BUDGET_CHARS = 4000;
/** One-line consensus summaries for the prompt (most recent first).
 *
 * KV-cache stability: this session's own digests are excluded. The digest
 * watcher writes a new entry whenever the session idles ~60s, so an active
 * conversation would otherwise change the injected bytes every round and
 * break the request prefix (cache miss) — while adding nothing the model
 * does not already have in the transcript.
 */
export declare function consensusPromptLines(mirror: DirectoryMirror, currentSessionId?: string): string[];
/** Static workflow guide, registered as a system section so the model
 * always knows how to reach shared memory, even on turns where the
 * change-detected snapshot is not re-injected. */
export declare const WORKFLOW_GUIDE: string;
/** Ranked directory index lines: working > recently active > recent rows. */
export declare function directoryPromptLines(mirror: DirectoryMirror, currentSessionId?: string): string[];
/**
 * Build the two bounded prompt sections for one assembly. Total text stays
 * under {@link PROMPT_BUDGET_CHARS}; entries are dropped from the directory
 * index first, then the consensus list, until the budget fits.
 */
export declare function buildSharedContextSections(mirror: DirectoryMirror, currentSessionId?: string): Array<{
    name: string;
    text: string;
}>;

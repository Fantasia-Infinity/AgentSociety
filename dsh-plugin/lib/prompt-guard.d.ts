/**
 * `agent-society-directory-index`: bounded shared-context injection guard.
 *
 * Runs after the ordinary system-prompt assembly (prepend listener) and
 * appends two bounded sections — the consensus one-liners and the session/
 * agent directory index — read from the local mirror maintained by the
 * directory sync plugin. The combined text stays under 4KB; on any failure
 * the original assembly is returned unchanged (same fail-safe philosophy as
 * the tool guards).
 *
 * KV-cache contract: the injection only APPENDS to the assembled sections
 * (the core system prefix is never touched), and the rendered text changes
 * only on semantic events — a new consensus digest or a session status/title
 * change. Idle churn (mirror refreshes, last_active_at) must not alter the
 * bytes; directory rows therefore sort stably by session_id. The section is
 * model-facing only: it is never written into the session log, so no UI
 * surface renders it as a user-visible message.
 */
import type { Context } from '@deepseek-ai/cordis';
export declare const name = "agent-society-directory-index";
export declare const inject: string[];
export declare function apply(ctx: Context): void;

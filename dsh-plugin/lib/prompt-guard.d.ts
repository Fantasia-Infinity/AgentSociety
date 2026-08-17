/**
 * `agent-society-directory-index`: bounded shared-context injection guard.
 *
 * Runs after the ordinary system-prompt assembly (prepend listener) and
 * appends two bounded sections — the consensus one-liners and the session/
 * agent directory index — read from the local mirror maintained by the
 * directory sync plugin. The combined text stays under 4KB; on any failure
 * the original assembly is returned unchanged (same fail-safe philosophy as
 * the tool guards).
 */
import type { Context } from '@deepseek-ai/cordis';
export declare const name = "agent-society-directory-index";
export declare const inject: string[];
export declare function apply(ctx: Context): void;

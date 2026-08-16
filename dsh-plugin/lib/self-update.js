/**
 * Self-update support for the in-process dsh worker plugin.
 *
 * A Hub task with `input.action === "self_update"` is handled by the worker
 * process itself, exactly like the Pi worker path: it pulls the AgentSociety
 * repository, installs changed dependencies, rebuilds both runtimes, reports
 * the result to the Hub, and exits with a dedicated code that makes the CLI
 * parent restart the dsh worker.
 */
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, readdirSync, statSync, } from 'node:fs';
import { dirname, resolve } from 'node:path';
export const SELF_UPDATE_ACTION = 'self_update';
export const SELF_UPDATE_EXIT_CODE = 75;
const SELF_UPDATE_BRANCH_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._/-]*$/u;
export function isSelfUpdateTask(task) {
    return task.input?.action === SELF_UPDATE_ACTION;
}
export async function runPluginSelfUpdate(task, options) {
    if (!options.enabled) {
        throw new Error('Self-update is disabled on this host (AGENT_SELF_UPDATE=0)');
    }
    const steps = [];
    const record = (label, output) => {
        const trimmed = output.trim();
        steps.push(trimmed ? `${label}: ${trimmed.slice(0, 800)}` : label);
    };
    const repositoryRoot = options.repositoryRoot;
    const pluginDir = resolve(repositoryRoot, 'dsh-plugin');
    const agentHostDir = resolve(repositoryRoot, 'agent-host');
    if (!existsSync(resolve(repositoryRoot, '.git')) && !existsSync(resolve(repositoryRoot, 'agent-host', 'package.json'))) {
        throw new Error(`Self-update repository root not found: ${repositoryRoot}`);
    }
    const branch = selfUpdateBranch(task);
    const npm = resolveNpm(options.nodePath);
    const before = (await run(repositoryRoot, 'git', ['rev-parse', '--short', 'HEAD'], 'Current commit', record)).trim();
    await run(repositoryRoot, 'git', ['fetch', 'origin'], 'Fetch origin', record);
    await run(repositoryRoot, 'git', ['pull', '--ff-only', 'origin', branch], `Pull origin/${branch}`, record);
    const after = (await run(repositoryRoot, 'git', ['rev-parse', '--short', 'HEAD'], 'Updated commit', record)).trim();
    const updated = after !== before;
    const stale = needsRebuild(agentHostDir, ['dist']) ||
        needsRebuild(pluginDir, ['lib']);
    await run(agentHostDir, options.nodePath, ['scripts/patch-pi-brace-expansion.mjs'], 'Apply security patch', record);
    await run(agentHostDir, options.nodePath, ['scripts/patch-pi-brace-expansion.mjs', '--check'], 'Verify security patch', record);
    if (updated) {
        await runNpm(npm, pluginDir, ['ci', '--ignore-scripts'], 'dsh-plugin npm ci', record);
        await runNpm(npm, agentHostDir, ['ci', '--ignore-scripts'], 'agent-host npm ci', record);
        await run(agentHostDir, options.nodePath, ['scripts/patch-pi-brace-expansion.mjs'], 'Apply security patch after npm ci', record);
    }
    if (updated || stale) {
        await buildTypeScript(npm, options.nodePath, pluginDir, 'dsh-plugin build', record);
        await buildTypeScript(npm, options.nodePath, agentHostDir, 'agent-host build', record);
    }
    else {
        record('Build', 'already up to date');
    }
    return {
        ok: true,
        updated,
        needsRestart: updated || stale,
        steps,
        before,
        after,
    };
}
function selfUpdateBranch(task) {
    const requested = task.input?.branch;
    if (typeof requested !== 'string' || requested.trim() === '')
        return 'main';
    const branch = requested.trim();
    const invalid = branch.length > 200 ||
        branch.startsWith('-') ||
        !SELF_UPDATE_BRANCH_PATTERN.test(branch) ||
        branch.split('/').includes('..') ||
        branch.startsWith('/');
    if (invalid) {
        throw new Error('Self-update branch must match [A-Za-z0-9][A-Za-z0-9._/-]* without \'..\' segments');
    }
    return branch;
}
function needsRebuild(dir, outputDirs) {
    for (const outputDir of outputDirs) {
        const output = resolve(dir, outputDir);
        if (!existsSync(output))
            return true;
    }
    const cli = resolve(dir, 'dist', 'src', 'cli.js');
    const plugin = resolve(dir, 'lib', 'worker-plugin.js');
    const marker = existsSync(cli) ? cli : plugin;
    const markerTime = statSync(marker).mtimeMs;
    const srcDir = resolve(dir, 'src');
    if (!existsSync(srcDir))
        return false;
    return latestSourceTime(srcDir) > markerTime;
}
function latestSourceTime(dir) {
    let latest = 0;
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const entryPath = resolve(dir, entry.name);
        if (entry.isDirectory()) {
            latest = Math.max(latest, latestSourceTime(entryPath));
        }
        else if (entry.isFile() &&
            (entry.name.endsWith('.ts') || entry.name.endsWith('.mjs'))) {
            latest = Math.max(latest, statSync(entryPath).mtimeMs);
        }
    }
    return latest;
}
function resolveNpm(nodePath) {
    if (process.platform === 'win32') {
        const probe = spawnSync('where.exe', ['npm'], { encoding: 'utf8' });
        if (probe.status === 0) {
            const line = (probe.stdout ?? '').trim().split(/\r?\n/u)[0];
            if (line) {
                const cli = resolve(dirname(line), 'node_modules', 'npm', 'bin', 'npm-cli.js');
                if (existsSync(cli))
                    return { command: nodePath, args: [cli] };
            }
        }
        return { command: 'npm.cmd', args: [] };
    }
    return { command: 'npm', args: [] };
}
async function buildTypeScript(npm, nodePath, dir, label, record) {
    const tsc = resolve(dir, 'node_modules', 'typescript', 'bin', 'tsc');
    if (existsSync(tsc)) {
        await run(dir, nodePath, [tsc, '-p', 'tsconfig.json'], label, record);
        return;
    }
    await runNpm(npm, dir, ['run', 'build'], label, record);
}
async function runNpm(npm, cwd, args, label, record) {
    await run(cwd, npm.command, [...npm.args, ...args], label, record);
}
async function run(cwd, command, args, label, record) {
    const timeoutMs = 10 * 60 * 1000;
    const child = spawn(command, args, {
        cwd,
        env: process.env,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => {
        stdout += chunk.toString();
        if (stdout.length > 32 * 1024 * 1024)
            child.kill();
    });
    child.stderr.on('data', (chunk) => {
        stderr += chunk.toString();
        if (stderr.length > 32 * 1024 * 1024)
            child.kill();
    });
    const timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs);
    const status = await new Promise((resolveExit, reject) => {
        child.once('error', reject);
        child.once('exit', (code, signal) => {
            if (signal)
                reject(new Error(`${label} timed out or was terminated (${signal})`));
            else
                resolveExit(code);
        });
    });
    clearTimeout(timer);
    const output = `${stdout}${stderr}`.trim();
    if (status !== 0) {
        throw new Error(`${label} failed (exit ${status ?? '?'}): ${output.slice(-2000)}`);
    }
    record(label, output);
    return output;
}
//# sourceMappingURL=self-update.js.map
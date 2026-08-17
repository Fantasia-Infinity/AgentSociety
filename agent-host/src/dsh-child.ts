import { spawn, type ChildProcess } from 'node:child_process'

export interface DshChildOptions {
  /**
   * Working directory for the child. Defaults to the parent's cwd, which is
   * the agent-host package dir under `npm --prefix`; interactive surfaces
   * pass INIT_CWD so the TUI's default workspace is the user's terminal
   * directory, not the installation.
   */
  cwd?: string
  /**
   * Handle a spawn error. Return `false` to report the child as not started
   * (caller usually returns false from the launcher). Any other return value
   * keeps the launcher alive as if the process started but failed.
   */
  onError?: (error: Error) => boolean | void
  /**
   * Handle an exit. Return `true` to request a restart (used by the plugin
   * worker self-update path).
   */
  onExit?: (code: number | null, signal: NodeJS.Signals | null) => boolean | void
}

export interface DshChildResult {
  /** Whether the child process was considered started. */
  started: boolean
  /** Whether the caller should restart the child. */
  restart: boolean
}

/**
 * Run one dsh child process with the standard signal forwarding and exit-code
 * handling shared by TUI, Web, worker, and dispatch launchers.
 */
export function runDshChild(
  command: readonly string[],
  env: NodeJS.ProcessEnv,
  options: DshChildOptions = {},
): Promise<DshChildResult> {
  return new Promise<DshChildResult>((resolveResult) => {
    const child: ChildProcess = spawn(command[0]!, command.slice(1), {
      stdio: 'inherit',
      env,
      ...(options.cwd !== undefined ? { cwd: options.cwd } : {}),
    })

    let settled = false
    const finish = (started: boolean, restart = false): void => {
      if (settled) return
      settled = true
      resolveResult({ started, restart })
    }

    const forwardSignal = (signal: NodeJS.Signals): void => {
      child.kill(signal)
    }
    const cleanup = (): void => {
      process.removeListener('SIGINT', forwardSignal)
      process.removeListener('SIGTERM', forwardSignal)
    }

    process.once('SIGINT', forwardSignal)
    process.once('SIGTERM', forwardSignal)

    child.once('error', (error: Error) => {
      cleanup()
      const handled = options.onError?.(error)
      finish(handled !== false)
    })

    child.once('exit', (code, signal) => {
      cleanup()
      const restart = options.onExit?.(code, signal) === true
      if (code) process.exitCode = code
      if (signal && ['SIGINT', 'SIGTERM'].includes(signal) === false) {
        process.exitCode = 1
      }
      finish(true, restart)
    })
  })
}

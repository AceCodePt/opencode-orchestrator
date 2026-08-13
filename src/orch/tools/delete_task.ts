import { tool } from "@opencode-ai/plugin"

// Absolute path to the orch dispatcher, baked in by the orch installer.
const TASKTOOL = "{{ORCH_BIN}}"

async function runTool(op: string, args: Record<string, unknown>, worktree: string): Promise<string> {
  const proc = Bun.spawn(
    // The spec JSON rides stdin (--json -) so a rich task can never trip the
    // command-line length limit; only the fixed parts stay in argv.
    [TASKTOOL, "tasktool", op, "--json", "-", "--dir", worktree],
    { stdin: "pipe", stdout: "pipe", stderr: "pipe" },
  )
  proc.stdin.write(JSON.stringify(args))
  await proc.stdin.end()
  const [stdout, stderr] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
  ])
  const exit = await proc.exited
  if (exit !== 0) {
    const detail = stderr.trim() || stdout.trim() || `tasktool exited ${exit}`
    throw new Error(detail)
  }
  return stdout.trim()
}

export default tool({
  description:
    "Delete a task. Removes tasks/<name>/task.md (a committed task is git-rm'd, " +
    "an uncommitted draft is deleted) or an archived copy if the task was already " +
    "archived. Staged, never committed - the human commits. Refuses claimed tasks " +
    "(the daemon owns claimed work) and refuses while any open task depends on it.",
  args: {
    name: tool.schema.string().describe("the task slug to delete"),
  },
  async execute(args, context) {
    return runTool("delete", args, context.worktree)
  },
})

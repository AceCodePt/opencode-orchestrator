import { tool } from "@opencode-ai/plugin"

// Absolute path to the orch dispatcher, baked in by the orch installer.
const TASKTOOL = "{{ORCH_BIN}}"

async function runTool(op: string, args: Record<string, unknown>, worktree: string): Promise<string> {
  const proc = Bun.spawn(
    [TASKTOOL, "tasktool", op, "--json", JSON.stringify(args), "--dir", worktree],
    { stdout: "pipe", stderr: "pipe" },
  )
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
    "Archive a declared, unclaimed task: git mv tasks/<name>/task.md to " +
    "archive/<name>/task.md in the base worktree, which unblocks anything that " +
    "depends on it. Staged, never committed - the human commits. Refuses claimed " +
    "tasks (the daemon owns claimed work) and uncommitted drafts.",
  args: {
    name: tool.schema.string().describe("the task slug to archive (tasks/<name>/task.md)"),
  },
  async execute(args, context) {
    return runTool("archive", args, context.worktree)
  },
})

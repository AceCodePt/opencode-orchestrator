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
    "Amend an uncommitted task draft in the project's base worktree. PARTIAL: " +
    "only pass the fields that change - absent fields are left untouched. " +
    "Refuses committed tasks (commit is a human act) and refuses when nothing is passed. " +
    "Never commits: the draft stays uncommitted for the human to review.",
  args: {
    name: tool.schema.string().describe("the task slug to amend (tasks/<name>/task.md)"),
    title: tool.schema.string().optional(),
    wait_human_start: tool.schema.boolean().optional().describe("true = a human must start it"),
    wait_human_merge: tool.schema.boolean().optional().describe("true = verified work waits for a human merge"),
    dependencies: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe("replace the full dependency list"),
    complexity: tool.schema.enum(["low", "medium", "high"]).optional(),
    priority: tool.schema.enum(["low", "medium", "high"]).optional(),
    context: tool.schema.string().optional(),
    requirements: tool.schema.array(tool.schema.string()).optional().describe("replace the full requirement list"),
    verification: tool.schema.string().optional(),
    prohibited_patterns: tool.schema.array(tool.schema.string()).optional().describe("replace the full list"),
  },
  async execute(args, context) {
    return runTool("update", args, context.worktree)
  },
})

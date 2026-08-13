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
    "Amend a task spec in the project's base worktree (committed or draft). PARTIAL: " +
    "only pass the fields that change - absent fields are left untouched. " +
    "Commits the amendment to base unless the spec's wait_human_start is true (a human " +
    "gate keeps it a draft for review); pass commit to override. " +
    "Refuses a claimed task (branch exists) and refuses when nothing is passed.",
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
    commit: tool.schema
      .boolean()
      .optional()
      .describe("commit the amendment to base (default: not wait_human_start); false leaves it uncommitted"),
  },
  async execute(args, context) {
    return runTool("update", args, context.worktree)
  },
})

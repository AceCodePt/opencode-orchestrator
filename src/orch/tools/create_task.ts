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
    "Create a task spec at tasks/<name>/task.md in the project's base worktree. " +
    "Never commits: the file is left uncommitted for the human to review and commit. " +
    "Pass wait_human_start and wait_human_merge as you and the human decided in the " +
    "conversation; they default to false (scheduler-eligible, auto-merge) when not " +
    "passed. Refuses to overwrite an existing task.\n\n" +
    "Decompose before you create: decide whether the description is ONE task or " +
    "several. A single effort gets a spec task (the anchor; Requirements are the " +
    "user stories, typically wait_human_start: true) plus one vertical slice task " +
    "per independent deliverable, each a COMPLETE path through every layer sized " +
    "to one context window. Give each slice a dependencies list naming the spec " +
    "task and any earlier slices; the scheduler only dispatches a slice once every " +
    "dependency is archived, so a slug that never gets written is a permanently " +
    "blocked task. A wide mechanical refactor is NOT a vertical slice: sequence it " +
    "expand-contract (add the new form, migrate call sites in batches, then delete " +
    "the old form). Describe WHAT the system should do, not HOW; every task needs a " +
    "Verification the project's hook could actually run; put the description's " +
    "checkboxes into Requirements. Never create a TASKS.md and never touch anything " +
    "outside tasks/.",
  args: {
    name: tool.schema
      .string()
      .describe("task slug; becomes tasks/<name>/task.md"),
    title: tool.schema.string().describe("human-readable task title (default: derived from name)"),
    wait_human_start: tool.schema
      .boolean()
      .optional()
      .describe("true = a human must `orch task start` it; the scheduler skips it. Default false"),
    wait_human_merge: tool.schema
      .boolean()
      .optional()
      .describe("true = verified work waits for a human `orch task merge`. Default false"),
    dependencies: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe("task slugs this task blocks on; the scheduler dispatches it only once every dependency is archived"),
    complexity: tool.schema.enum(["low", "medium", "high"]).optional(),
    priority: tool.schema.enum(["low", "medium", "high"]).optional(),
    context: tool.schema.string().optional().describe("why this task exists, from the conversation"),
    requirements: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe("concrete, checkable requirements"),
    verification: tool.schema
      .string()
      .optional()
      .describe("the observable behaviour proving it is done - make it runnable"),
    prohibited_patterns: tool.schema
      .array(tool.schema.string())
      .optional()
      .describe("what not to do, and why"),
  },
  async execute(args, context) {
    return runTool("create", args, context.worktree)
  },
})

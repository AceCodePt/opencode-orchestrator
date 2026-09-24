// No import: OpenCode 2 accepts a plugin as a plain default-exported object
// `{ id, setup }`. Importing `@opencode/plugin` is documented but the package is
// not resolvable on every install, which makes the plugin fail to load, so this
// is written against the raw context to stay loadable everywhere.

// Absolute path to the orch dispatcher, baked in by the orch installer.
const TASKTOOL = "{{ORCH_BIN}}"

type JsonSchema = Record<string, unknown>
type ToolEditor = { add: (tool: unknown) => void }
type PluginContext = {
  location: { directory: string }
  tool: { transform: (cb: (editor: ToolEditor) => void) => Promise<unknown> }
}

// The four task-intake tools, registered through the V2 tool transform. They are
// thin bridges to the same Python backend as the CLI (`orch tasktool`), so
// slugs, front matter, dependencies and refusals share orch's code.

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

const COMMON_FLAGS: JsonSchema = {
  wait_human_start: {
    type: "boolean",
    description: "true = a human must `orch task start` it; the scheduler skips it. Default false",
  },
  wait_human_merge: {
    type: "boolean",
    description: "true = verified work waits for a human `orch task merge`. Default false",
  },
  commit: {
    type: "boolean",
    description: "commit the change to base (default: not wait_human_start); false leaves it uncommitted",
  },
}

const CREATE_DESCRIPTION =
  "Create a task spec at tasks/<name>/task.md in the project's base (primary) " +
  "worktree. May be run from any worktree of the project - the spec always lands " +
  "in the base worktree, never the calling worktree. " +
  "Commits it to the base branch when wait_human_start is false, so the daemon sees " +
  "it and the loop can run on its own; with wait_human_start: true it stays an " +
  "uncommitted draft for the human to review. Pass commit to override. " +
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
  "outside tasks/."

function registerTools(ctx: PluginContext, editor: ToolEditor): void {
  const fallback = ctx.location.directory

  const worktreeOf = (context: any): string =>
    context?.location?.directory || context?.directory || fallback

  editor.add({
    name: "create_task",
    description: CREATE_DESCRIPTION,
    input: {
      type: "object",
      properties: {
        name: { type: "string", description: "task slug; becomes tasks/<name>/task.md" },
        title: { type: "string", description: "human-readable task title (default: derived from name)" },
        ...COMMON_FLAGS,
        dependencies: {
          type: "array",
          items: { type: "string" },
          description: "task slugs this task blocks on; the scheduler dispatches it only once every dependency is archived",
        },
        complexity: { type: "string", enum: ["low", "medium", "high"] },
        priority: { type: "string", enum: ["low", "medium", "high"] },
        context: { type: "string", description: "why this task exists, from the conversation" },
        requirements: {
          type: "array",
          items: { type: "string" },
          description: "concrete, checkable requirements",
        },
        verification: {
          type: "string",
          description: "the observable behaviour proving it is done - make it runnable",
        },
        prohibited_patterns: {
          type: "array",
          items: { type: "string" },
          description: "what not to do, and why",
        },
      },
      required: ["name"],
      additionalProperties: false,
    },
    async execute(input: Record<string, unknown>, context: any) {
      return { content: await runTool("create", input, worktreeOf(context)) }
    },
  })

  editor.add({
    name: "update_task",
    description:
      "Amend a task spec in the project's base worktree (committed or draft). PARTIAL: " +
      "only pass the fields that change - absent fields are left untouched. " +
      "Commits the amendment to base unless the spec's wait_human_start is true (a human " +
      "gate keeps it a draft for review); pass commit to override. " +
      "Refuses a claimed task (branch exists) and refuses when nothing is passed.",
    input: {
      type: "object",
      properties: {
        name: { type: "string", description: "the task slug to amend (tasks/<name>/task.md)" },
        title: { type: "string" },
        ...COMMON_FLAGS,
        dependencies: {
          type: "array",
          items: { type: "string" },
          description: "replace the full dependency list",
        },
        complexity: { type: "string", enum: ["low", "medium", "high"] },
        priority: { type: "string", enum: ["low", "medium", "high"] },
        context: { type: "string" },
        requirements: {
          type: "array",
          items: { type: "string" },
          description: "replace the full requirement list",
        },
        verification: { type: "string" },
        prohibited_patterns: {
          type: "array",
          items: { type: "string" },
          description: "replace the full list",
        },
      },
      required: ["name"],
      additionalProperties: false,
    },
    async execute(input: Record<string, unknown>, context: any) {
      return { content: await runTool("update", input, worktreeOf(context)) }
    },
  })

  editor.add({
    name: "archive_task",
    description:
      "Archive a declared, unclaimed task: git mv tasks/<name>/task.md to " +
      "archive/<name>/task.md in the base worktree, which unblocks anything that " +
      "depends on it. A slug is freely reusable: if an earlier generation of the " +
      "slug already occupies archive/<name>/task.md, this generation is archived " +
      "to a timestamped sub-path archive/<name>/<utc-ts>/task.md instead. Commits " +
      "the move to base by default; pass commit: false to leave it staged for " +
      "review. Refuses claimed tasks (the daemon owns claimed work) and uncommitted drafts.",
    input: {
      type: "object",
      properties: {
        name: { type: "string", description: "the task slug to archive (tasks/<name>/task.md)" },
        commit: {
          type: "boolean",
          description: "commit the archive move to base (default true); false leaves it staged",
        },
      },
      required: ["name"],
      additionalProperties: false,
    },
    async execute(input: Record<string, unknown>, context: any) {
      return { content: await runTool("archive", input, worktreeOf(context)) }
    },
  })

  editor.add({
    name: "delete_task",
    description:
      "Delete a task. Removes tasks/<name>/task.md (a committed task is git-rm'd, " +
      "an uncommitted draft is deleted) or an archived copy if the task was already " +
      "archived. Commits the removal to base by default; pass commit: false to leave " +
      "it staged for review. Refuses claimed tasks (the daemon owns claimed work) and " +
      "refuses while any open task depends on it.",
    input: {
      type: "object",
      properties: {
        name: { type: "string", description: "the task slug to delete" },
        commit: {
          type: "boolean",
          description: "commit the removal to base (default true); false leaves it staged",
        },
      },
      required: ["name"],
      additionalProperties: false,
    },
    async execute(input: Record<string, unknown>, context: any) {
      return { content: await runTool("delete", input, worktreeOf(context)) }
    },
  })
}

export default {
  id: "orch.tasks",
  async setup(ctx: PluginContext) {
    await ctx.tool.transform((editor) => {
      registerTools(ctx, editor)
    })
  },
}

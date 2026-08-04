# Task Runner Hooks

This directory contains extensible hook scripts that are called by `task-runner.sh` at various stages of the task lifecycle.

## Hook Execution Order

### Task Creation

1. `pre-task-create.sh` - Before worktree creation
2. _(task-runner creates worktree)_
3. `post-task-create.sh` - After worktree creation (creates database, updates .env)

### Task Cleanup

1. `pre-task-cleanup.sh` - Before worktree deletion
2. _(cleanup script deletes worktree)_
3. `post-task-cleanup.sh` - After worktree deletion (deletes database)

## Environment Variables

Each hook receives these environment variables:

| Variable        | Description               | Example                                             |
| --------------- | ------------------------- | --------------------------------------------------- |
| `TASK_NAME`     | The task name             | `favorites-feature`                                 |
| `BRANCH_NAME`   | The git branch name       | `feature/favorites-feature`                         |
| `WORKTREE_PATH` | Absolute path to worktree | `/home/user/rdk-template/feature-favorites-feature` |
| `WORKTREE_NAME` | Directory name            | `feature-favorites-feature`                         |

## Customization

### Adding Custom Logic

You can add custom logic to any hook. For example:

**pre-task-create.sh** - Validate task name format:

```bash
if [[ ! "$TASK_NAME" =~ ^[a-z0-9-]+$ ]]; then
  echo "❌ Invalid task name. Use lowercase letters, numbers, and hyphens only."
  exit 1
fi
```

**post-task-create.sh** - Install additional dependencies:

```bash
cd "$WORKTREE_PATH"
pnpm install --frozen-lockfile
pnpm build
```

**pre-task-cleanup.sh** - Backup important files:

```bash
if [ -f "$WORKTREE_PATH/important-data.json" ]; then
  cp "$WORKTREE_PATH/important-data.json" ~/backups/
fi
```

### Disabling Hooks

To disable a hook, simply make it non-executable:

```bash
chmod -x hooks/post-task-create.sh
```

Or rename it:

```bash
mv hooks/post-task-create.sh hooks/post-task-create.sh.disabled
```

### Creating Additional Hooks

You can create additional hooks for specific use cases:

```bash
# hooks/post-task-create-notify.sh
#!/bin/bash
# Send notification when task is created
notify-send "Task Created" "Database ready for ${TASK_NAME}"
```

Then call it from the main hook:

```bash
# hooks/post-task-create.sh
# ... existing logic ...

# Call additional hooks
if [ -x "$(dirname "$0")/post-task-create-notify.sh" ]; then
  bash "$(dirname "$0")/post-task-create-notify.sh"
fi
```

## Testing Hooks

Test hooks in isolation:

```bash
# Set environment variables
export TASK_NAME="test-task"
export BRANCH_NAME="feature/test-task"
export WORKTREE_PATH="/tmp/test-worktree"
export WORKTREE_NAME="feature-test-task"

# Run hook
bash hooks/post-task-create.sh
```

## Troubleshooting

### Hook Not Running

1. Check if the hook is executable: `ls -l hooks/`
2. Check for syntax errors: `bash -n hooks/post-task-create.sh`
3. Run with debug output: `bash -x hooks/post-task-create.sh`

### Hook Failing

Hooks use `set -e`, so they exit on first error. To debug:

```bash
# Remove set -e temporarily
# set -e

# Add debug output
set -x
```

## Best Practices

1. **Keep hooks fast** - They run on every task creation/cleanup
2. **Handle errors gracefully** - Don't block task creation for non-critical failures
3. **Log important actions** - Use `echo` to show what's happening
4. **Make hooks idempotent** - Safe to run multiple times
5. **Document custom logic** - Add comments explaining why

#!/bin/bash
# pre-task-cleanup.sh - Hook called before worktree deletion
# Use this for any cleanup that needs to happen before the worktree is removed
#
# Environment variables provided by cleanup script:
#   TASK_NAME       - The task name
#   BRANCH_NAME     - The git branch name
#   WORKTREE_PATH   - Absolute path to the worktree
#   WORKTREE_NAME   - Directory name of the worktree

set -e

# Add your custom pre-cleanup logic here
# Examples:
# - Backup important files
# - Check for uncommitted changes
# - Verify task is complete
# - etc.

# Example: Warn if there are uncommitted changes
if [ -d "$WORKTREE_PATH" ]; then
  cd "$WORKTREE_PATH"
  if ! git diff-index --quiet HEAD -- 2>/dev/null; then
    echo "⚠️  Warning: Worktree has uncommitted changes"
    git status --short
    read -p "   Continue with cleanup? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
      exit 1
    fi
  fi
fi

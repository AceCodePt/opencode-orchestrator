#!/bin/bash
# pre-task-create.sh - Hook called before worktree creation
# Use this for any setup that needs to happen before the worktree exists
#
# Environment variables provided by task-runner:
#   TASK_NAME       - The task name (e.g., "favorites-feature")
#   BRANCH_NAME     - The git branch name (e.g., "feature/favorites-feature")

set -e

# Example: Check if PostgreSQL is running
CONTAINER_NAME="${CONTAINER_NAME:-reeeally-postgres}"

if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  echo "⚠️  Warning: PostgreSQL container is not running"
  echo "   Start it with: cd reeeally-main && docker-compose up -d"
  read -p "   Continue anyway? (y/n) " -n 1 -r
  echo
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 1
  fi
fi

# Add your custom pre-creation logic here
# Examples:
# - Validate task name format
# - Check for required dependencies
# - Verify disk space
# - etc.

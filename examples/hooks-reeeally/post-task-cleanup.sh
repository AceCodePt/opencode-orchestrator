#!/bin/bash
# post-task-cleanup.sh - Hook called after worktree deletion
# This deletes the task-specific database
#
# Environment variables provided by cleanup script:
#   TASK_NAME       - The task name
#   BRANCH_NAME     - The git branch name
#   WORKTREE_NAME   - Directory name of the worktree

set -e

CONTAINER_NAME="${CONTAINER_NAME:-reeeally-postgres}"

# Normalize branch name to database name
NORMALIZED_BRANCH="${BRANCH_NAME//\//-}"
DB_NAME="reeeally_${NORMALIZED_BRANCH//-/_}"

echo ""
echo "🗑️  Cleaning up database for task..."

# Check if container is running
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  echo "⚠️  PostgreSQL container is not running, skipping database cleanup"
  exit 0
fi

# Check if database exists
if ! docker exec "$CONTAINER_NAME" psql -U postgres -lqt | cut -d \| -f 1 | grep -qw "$DB_NAME"; then
  echo "ℹ️  Database '${DB_NAME}' does not exist, nothing to clean up"
  exit 0
fi

# Check if running in non-interactive mode (background process)
if [ ! -t 0 ]; then
  # Non-interactive mode - auto-delete
  echo "   Database: ${DB_NAME}"
  echo "   Running in non-interactive mode, auto-deleting..."
  if docker exec "$CONTAINER_NAME" psql -U postgres -c "DROP DATABASE ${DB_NAME};" >/dev/null 2>&1; then
    echo "✅ Database deleted successfully"
  else
    echo "❌ Failed to delete database"
    exit 1
  fi
else
  # Interactive mode - ask for confirmation
  echo "   Database: ${DB_NAME}"
  read -p "   Delete this database? (y/n) " -n 1 -r
  echo
  
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    if docker exec "$CONTAINER_NAME" psql -U postgres -c "DROP DATABASE ${DB_NAME};" >/dev/null 2>&1; then
      echo "✅ Database deleted successfully"
    else
      echo "❌ Failed to delete database"
      exit 1
    fi
  else
    echo "⏭️  Skipped database deletion"
    echo "   You can delete it manually later with:"
    echo "   docker exec ${CONTAINER_NAME} psql -U postgres -c 'DROP DATABASE ${DB_NAME};'"
  fi
fi

echo ""

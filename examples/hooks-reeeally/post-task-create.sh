#!/bin/bash
# post-task-create.sh - Hook called after worktree creation
# This creates the task-specific database and updates .env
#
# Environment variables provided by task-runner:
#   TASK_NAME       - The task name (e.g., "favorites-feature")
#   BRANCH_NAME     - The git branch name (e.g., "feature/favorites-feature")
#   WORKTREE_PATH   - Absolute path to the worktree
#   WORKTREE_NAME   - Directory name of the worktree

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Normalize branch name to database name
# feature/favorites-feature -> reeeally_feature_favorites_feature
NORMALIZED_BRANCH="${BRANCH_NAME//\//-}"
DB_NAME="reeeally_${NORMALIZED_BRANCH//-/_}"

echo ""
echo "🗄️  Setting up database for task..."

# Create database using the standalone script
if [ -f "$PROJECT_ROOT/scripts/db-create.sh" ]; then
  bash "$PROJECT_ROOT/scripts/db-create.sh" "$DB_NAME" & disown
else
  echo "⚠️  Warning: db-create.sh not found, skipping database creation"
  exit 0
fi

# Update database configuration in every env file that carries it.
# The web app's effective DB config comes from .env.local.web;
# the worker's from .env.local.shared. Both must point at the task DB.
DB_ENV_FILES=(
  "${WORKTREE_PATH}/.env.local.shared"
  "${WORKTREE_PATH}/.env.local.web"
  "${WORKTREE_PATH}/.env.local.public"
)

UPDATED_ANY=false

for ENV_FILE in "${DB_ENV_FILES[@]}"; do
  [ -f "$ENV_FILE" ] || continue
  UPDATED_ANY=true
  echo "📝 Updating ${ENV_FILE##*/} with database configuration..."

  # Update or add PGDATABASE line
  if grep -q "^PGDATABASE=" "$ENV_FILE"; then
    sed -i "s|^PGDATABASE=.*|PGDATABASE=\"${DB_NAME}\"|" "$ENV_FILE"
  else
    echo "PGDATABASE=\"${DB_NAME}\"" >> "$ENV_FILE"
  fi

  # Ensure PGHOST is localhost
  if grep -q "^PGHOST=" "$ENV_FILE"; then
    sed -i 's|^PGHOST=.*|PGHOST="localhost"|' "$ENV_FILE"
  else
    echo 'PGHOST="localhost"' >> "$ENV_FILE"
  fi

  # Ensure PGUSER is postgres
  if grep -q "^PGUSER=" "$ENV_FILE"; then
    sed -i 's|^PGUSER=.*|PGUSER="postgres"|' "$ENV_FILE"
  else
    echo 'PGUSER="postgres"' >> "$ENV_FILE"
  fi

  echo "   PGHOST=localhost"
  echo "   PGDATABASE=${DB_NAME}"
  echo "   PGUSER=postgres"
done

if [ "$UPDATED_ANY" = false ]; then
  echo "⚠️  Warning: no .env.local.* file found at ${WORKTREE_PATH} to configure"
fi

echo ""

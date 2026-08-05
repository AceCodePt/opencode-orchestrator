#!/bin/bash

# task-runner.sh - Streamlined Task Initialization
# Usage: ../task-runner.sh (from main/) or ./task-runner.sh (from parent)

if [ -d "reeeally-preview" ]; then
    BASE_DIR="."
    MAIN_DIR="reeeally-preview"
elif [ -f "package.json" ] && [ "$(basename $(pwd))" = "reeeally-preview" ]; then
    BASE_DIR=".."
    MAIN_DIR="."
else
    echo "Error: Could not find reeeally-preview directory. Run this from the parent directory or from within reeeally-preview."
    exit 1
fi

TASKS_FILE="$BASE_DIR/reeeally-preview/TASKS.md"

if [ ! -f "$TASKS_FILE" ]; then
    echo "Error: $TASKS_FILE not found."
    exit 1
fi

# Use HEAD, not the working tree, so line numbers are stable
task_list=$(git show HEAD:TASKS.md | awk '/^- \[ \]/{printf "%d:%s\n", NR, $0}')

if [ -z "$task_list" ]; then
    echo "No available tasks found in $TASKS_FILE."
    exit 0
fi

IFS=$'\n' task_entries=($task_list)
unset IFS

tasks=()
task_names=()
task_folders=()

for entry in "${task_entries[@]}"; do
    task_line="${entry#*:}"
    tasks+=("$task_line")
    
    name=$(echo "$task_line" | sed -E 's/^- \[ \] \[([^]]*)\].*/\1/')
    path=$(echo "$task_line" | sed -E 's/^- \[ \] \[.*\]\(([^)]*)\).*/\1/')
    folder=$(basename "$(dirname "$path")")
    
    task_names+=("$name")
    task_folders+=("$folder")
done

# 3. Pick tasks
if [ $# -gt 0 ]; then
    choices_array=("$@")
else
    echo "Available Tasks:"
    echo "Detected ${#tasks[@]} available tasks from HEAD."
    for i in "${!tasks[@]}"; do
        echo "$((i+1))) ${tasks[$i]}"
    done
    echo ""
    printf "Pick task numbers, names, or folder names (space-separated): "
    read -r choices
    read -r -a choices_array <<< "$choices"
fi

for choice in "${choices_array[@]}"; do
    selected_index=-1
    
    if [[ "$choice" =~ ^[0-9]+$ ]]; then
        if [ "$choice" -ge 1 ] && [ "$choice" -le "${#tasks[@]}" ]; then
            selected_index=$((choice-1))
        fi
    fi
    
    if [ "$selected_index" -eq -1 ]; then
        for i in "${!tasks[@]}"; do
            if [ "$choice" = "${task_names[$i]}" ] || [ "$choice" = "${task_folders[$i]}" ]; then
                selected_index=$i
                break
            fi
        done
    fi
    
    if [ "$selected_index" -eq -1 ]; then
        echo "Warning: No match found for '$choice'. Skipping."
        continue
    fi
    
    task_name="${task_names[$selected_index]}"
    
    branch_name=$(echo "$task_name" | tr '[:upper:]' '[:lower:]' | sed 's/ /-/g' | sed 's/[^a-z0-9-]//g')

    echo "------------------------------------------"
    echo "Processing: $task_name"

    # Each subsequent task's worktree must be based on a commit that includes
    # the previous task's status, so mark it in-progress and commit first.
    echo "[$branch_name] Marking as in-progress..."
    escaped_task_name=$(echo "$task_name" | sed 's/[&/\"\]/\\&/g')
    sed -i "s/^- \[ \] \[$escaped_task_name\]/- [-] [$escaped_task_name]/" "$TASKS_FILE"
    
    (
        cd "$BASE_DIR/reeeally-preview"
        git add TASKS.md
        git commit -m "chore: mark task '$task_name' as in-progress" -q
    )

    echo "[$branch_name] Initializing worktree..."
    (
        cd "$BASE_DIR/reeeally-preview"
        zsh -c "source ~/.zshrc && gwadd $branch_name"
    )

    (
        TARGET_DIR="$BASE_DIR/$branch_name"
        if [ -d "$TARGET_DIR" ]; then
            PARENT_DIR_NAME=$(basename $(realpath "$BASE_DIR"))
            SESSION_NAME="$PARENT_DIR_NAME/$branch_name"

            echo "[$branch_name] Creating tmux session: $SESSION_NAME"

            # Pass parent-connection env vars only when set (empty -e values break tmux)
            TMUX_ENV_ARGS=()
            if [ -n "$OPENCODE_SESSION_ID" ]; then
                TMUX_ENV_ARGS+=(-e "OPENCODE_PARENT_SESSION_ID=$OPENCODE_SESSION_ID")
            fi
            if [ -n "$OPENCODE_SERVER_URL" ]; then
                TMUX_ENV_ARGS+=(-e "OPENCODE_PARENT_SERVER_URL=$OPENCODE_SERVER_URL")
            fi

            tmux new-session -d -s "$SESSION_NAME" -c "$TARGET_DIR" "${TMUX_ENV_ARGS[@]}"
            sleep 2
            tmux_task_name=$(printf %q "$task_name")
            # every tmux session opens nvim's terminal, which needs i for opencode
            tmux send-keys -t "$SESSION_NAME" "iopencode --agent team-lead --prompt \"You are the Team Lead. Read tasks/${task_folders[$selected_index]}/task.md and execute it. Make structural edits yourself (migrations, schemas, routes, registry). Delegate implementation to test-creator and screen sub-agents.\"" C-m  

            echo "[$branch_name] Ready."
        else
            echo "Error: Target directory $TARGET_DIR was not created."
        fi
    ) &
done

wait
echo "------------------------------------------"
echo "All tasks initialized."

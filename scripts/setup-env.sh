#!/bin/zsh
set -eu

REPO_DIR="${1:-$HOME/Coding/completed-games-tracker-fastapi}"

mkdir -p "$REPO_DIR/.github/instructions"
mkdir -p "$REPO_DIR/docs/agent-guides"
mkdir -p "$REPO_DIR/backend"
mkdir -p "$REPO_DIR/frontend"

touch "$REPO_DIR/README.md"
touch "$REPO_DIR/AGENTS.md"
touch "$REPO_DIR/.github/copilot-instructions.md"
touch "$REPO_DIR/.github/instructions/backend.instructions.md"
touch "$REPO_DIR/.github/instructions/frontend.instructions.md"
touch "$REPO_DIR/docs/agent-guides/build-test-verify.md"
touch "$REPO_DIR/docs/agent-guides/project-map.md"

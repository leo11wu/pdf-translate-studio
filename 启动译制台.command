#!/usr/bin/env bash

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
exec bash "$SCRIPT_DIR/scripts/dev-studio.sh"

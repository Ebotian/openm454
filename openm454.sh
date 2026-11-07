#!/bin/sh
# POSIX / Git Bash shim: ./openm454.sh capture -c A1
cd "$(dirname "$0")" && exec python -X utf8 -m openm454 "$@"

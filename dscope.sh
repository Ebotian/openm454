#!/bin/sh
# POSIX / Git Bash shim: ./dscope.sh capture -c A1
cd "$(dirname "$0")" && exec python -X utf8 -m dscope "$@"

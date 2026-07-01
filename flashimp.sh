#!/bin/sh
script_dir=`dirname $0`
exec uv run "$script_dir/flashimp.py" "$@"

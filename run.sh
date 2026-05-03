#!/bin/bash

set -o errexit
set -o nounset
set -o pipefail

main () {
    uv run main.py "$@"
}

main "$@"

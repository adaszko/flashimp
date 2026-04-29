#!/bin/bash

set -o errexit
set -o nounset
set -o pipefail

main () {
    exec claude --resume 4656ead3-8c27-48b2-92f5-a6d64045e3f6
}

main "$@"

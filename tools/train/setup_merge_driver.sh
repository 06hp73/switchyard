#!/usr/bin/env bash
# Register the key-path JSON merge driver in the LOCAL repo config.
# Must be run once per clone (config is not versioned). The train host runs it;
# GitHub's web merge ignores custom drivers, which is fine - merges happen on
# the train, locally.
set -eu
TOP=$(git rev-parse --show-toplevel)
git config merge.jsoncatalog.name "key-path JSON catalog merge"
git config merge.jsoncatalog.driver "/Users/storslasken/Developer/EV4XL-SIM/.venv/bin/python $TOP/tools/train/json_merge_driver.py %O %A %B"
echo "jsoncatalog merge driver registered for this clone."

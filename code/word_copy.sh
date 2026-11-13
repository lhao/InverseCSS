#!/bin/bash
# Word copy of a progress note for Hao (docs/word/). NEVER overwrites an existing .docx: Hao writes questions in them.
# If the note was extended after its Word copy was made, the new copy gets a suffix (_v2, _v3, ...).
#   usage: bash code/word_copy.sh docs/PROGRESS_NOTE_<date>_<time>.md
set -euo pipefail
src=${1:?note}; base=docs/word/$(basename "${src%.md}"); out=$base.docx; n=1
while [ -e "$out" ]; do n=$((n + 1)); out=${base}_v$n.docx; done
pandoc "$src" -o "$out" && echo "$out"

#!/usr/bin/env bash

set -ex

pushd docs/website
poetry install --no-root
popd

mkdir -p build/doc

OUT_BASE="$(readlink -f build/doc)"

poetry run -C docs/website -- sphinx-build -b html "$(readlink -f docs/website/src)" "$OUT_BASE/html"
poetry run -C docs/website -- sphinx-build -b text "$(readlink -f docs/website/src)" "$OUT_BASE/text"

ruby ./docs/website/merge-txts.rb "$OUT_BASE/text/python-sdk" "$OUT_BASE/html/_static/ai/api.txt"
ruby ./docs/website/merge-txts.rb "$OUT_BASE/text/spec" "$OUT_BASE/html/_static/ai/spec.txt"
ruby ./docs/website/merge-txts.rb "$OUT_BASE/text/impl-spec" "$OUT_BASE/html/_static/ai/impl-spec.txt"

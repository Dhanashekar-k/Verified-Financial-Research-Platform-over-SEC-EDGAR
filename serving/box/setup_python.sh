#!/usr/bin/env bash
# Create the Python 3.12 environment the loader and bench run in (on the box, as root, via SSM).
set -euo pipefail
cd /opt/edgarqa
[ -f bootstrap.done ] || { echo "bootstrap.done missing: user_data failed"; exit 1; }
[ -f code/serving/box/requirements.txt ] || { echo "code bundle missing: run pull_bundle.sh"; exit 1; }
[ -d venv ] || uv venv --python 3.12 venv
uv pip install --python venv/bin/python -r code/serving/box/requirements.txt
venv/bin/python -c "import psycopg, pyarrow, fastembed, onnxruntime; print('python deps ok', psycopg.__version__, pyarrow.__version__, onnxruntime.__version__)"

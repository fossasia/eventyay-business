#!/bin/sh
REPO_DIR=$(git rev-parse --show-toplevel)
GIT_DIR=$REPO_DIR/.git
VENV_ACTIVATE=$VIRTUAL_ENV/bin/activate
if [ ! -f "$VENV_ACTIVATE" ]
then
    echo "Could not find your virtual environment"
    exit 1
fi

HOOK_FILE="$GIT_DIR/hooks/pre-commit"
BLOCK_START="# --- EVENTYAY MANAGED BLOCK START ---"
BLOCK_END="# --- EVENTYAY MANAGED BLOCK END ---"

if [ -f "$HOOK_FILE" ]; then
    sed -i.bak "/$BLOCK_START/,/$BLOCK_END/d" "$HOOK_FILE"
    rm -f "$HOOK_FILE.bak"
else
    echo "#!/bin/sh" > "$HOOK_FILE"
    echo "set -e" >> "$HOOK_FILE"
fi

cat <<EOF >> "$HOOK_FILE"
$BLOCK_START
. "$VENV_ACTIVATE"
black --check .
isort -c .
flake8 .
$BLOCK_END
EOF
chmod +x "$HOOK_FILE"

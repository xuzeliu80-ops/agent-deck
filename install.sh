#!/bin/sh
set -eu
AW_HOME=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${AW_PYTHON:-python3}
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Python not found: $PYTHON (set AW_PYTHON to Python >= 3.9)" >&2
    exit 1
fi
"$PYTHON" -c 'import sys; assert sys.version_info >= (3,9), "需要 Python >= 3.9"'
"$PYTHON" -m venv "$AW_HOME/.venv"
"$AW_HOME/.venv/bin/python" -m pip install --disable-pip-version-check 'textual==6.5.0'
if mkdir -p "$HOME/.local/bin" && ln -sf "$AW_HOME/bin/aw" "$HOME/.local/bin/aw"; then
    echo '安装完成：aw（请确保 ~/.local/bin 在 PATH 中）'
else
    echo '环境已就绪；请手动运行：' >&2
    printf 'ln -sf "%s/bin/aw" "$HOME/.local/bin/aw"\n' "$AW_HOME" >&2
fi

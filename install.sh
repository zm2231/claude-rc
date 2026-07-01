#!/usr/bin/env bash
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [[ -L "$SOURCE" ]]; do
  DIR="$(cd -P -- "$(dirname -- "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
ROOT="$(cd -P -- "$(dirname -- "$SOURCE")" && pwd)"

python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python3" -m pip install --upgrade pip
"$ROOT/.venv/bin/python3" -m pip install -r "$ROOT/requirements.txt"
npm --prefix "$ROOT" install

mkdir -p "$HOME/.local/bin"
ln -sf "$ROOT/bin/claude-rc-send" "$HOME/.local/bin/claude-rc-send"
ln -sf "$ROOT/bin/claude-rc-list" "$HOME/.local/bin/claude-rc-list"

if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
  shell_name="$(basename "${SHELL:-}")"
  case "$shell_name" in
    zsh) rc_file="$HOME/.zshrc" ;;
    bash) rc_file="$HOME/.bashrc" ;;
    *) rc_file="$HOME/.profile" ;;
  esac
  marker="# claude-rc: add ~/.local/bin to PATH"
  touch "$rc_file"
  if ! grep -Fq "$marker" "$rc_file"; then
    {
      echo ""
      echo "$marker"
      echo 'export PATH="$HOME/.local/bin:$PATH"'
    } >> "$rc_file"
  fi
  path_note="added $HOME/.local/bin to PATH in $rc_file; restart your shell or run: export PATH=\"$HOME/.local/bin:\$PATH\""
else
  path_note="$HOME/.local/bin is already on PATH"
fi

echo "installed claude-rc"
echo "$path_note"
echo "run: claude-rc-send doctor"

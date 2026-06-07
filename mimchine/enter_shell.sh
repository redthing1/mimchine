shell_state_dir="${MIM_SHELL_STATE_DIR:-__SHELL_STATE_GUEST_DIR__}"
shell_state_enabled="${MIM_SHELL_STATE:-1}"

shell_state_available() {
  [ "$shell_state_enabled" != "0" ] && [ -d "$shell_state_dir" ]
}

shell_is_missing() {
  [ -z "$shell" ] || [ ! -x "$shell" ]
}

resolve_auto_shell() {
  shell="${SHELL:-}"

  if shell_is_missing; then
    user_name="$(id -un 2>/dev/null || true)"
    if [ -n "$user_name" ]; then
      shell="$(
        awk -F: -v user="$user_name" '
          $1 == user { print $7; exit }
        ' /etc/passwd 2>/dev/null || true
      )"
    fi
  fi

  if shell_is_missing; then
    if command -v zsh >/dev/null 2>&1; then
      shell="$(command -v zsh)"
    elif command -v bash >/dev/null 2>&1; then
      shell="$(command -v bash)"
    else
      shell="$(command -v sh || printf /bin/sh)"
    fi
  fi
}

write_zsh_state_files() {
  mim_zdotdir="$shell_state_dir/.mim-zdotdir"
  mkdir -p "$mim_zdotdir" || return 1
  export MIM_ORIGINAL_ZDOTDIR="${ZDOTDIR:-$HOME}"
  export MIM_SHELL_STATE_ZDOTDIR="$mim_zdotdir"

  cat > "$mim_zdotdir/.zshenv" <<'MIM_ZSHENV'
_mim_source_original_zdotfile() {
  emulate -L zsh
  local file="${MIM_ORIGINAL_ZDOTDIR:-$HOME}/$1"
  [[ "$file" != "$ZDOTDIR/$1" && -r "$file" ]] && source "$file"
}

_mim_source_original_zdotfile .zshenv
export ZDOTDIR="${MIM_SHELL_STATE_ZDOTDIR:-$ZDOTDIR}"
MIM_ZSHENV

  cat > "$mim_zdotdir/.zprofile" <<'MIM_ZPROFILE'
_mim_source_original_zdotfile .zprofile
MIM_ZPROFILE

  cat > "$mim_zdotdir/.zlogin" <<'MIM_ZLOGIN'
_mim_source_original_zdotfile .zlogin
MIM_ZLOGIN

  cat > "$mim_zdotdir/.zlogout" <<'MIM_ZLOGOUT'
_mim_source_original_zdotfile .zlogout
MIM_ZLOGOUT

  cat > "$mim_zdotdir/.zshrc" <<'MIM_ZSHRC'
_mim_source_original_zdotfile .zshrc

if [[ -n ${HISTFILE:-} ]]; then
  autoload -Uz add-zsh-hook

  _mim_shell_state_append_zsh_history() {
    emulate -L zsh
    [[ -n ${HISTFILE:-} && -n ${1:-} ]] || return 0
    umask 077
    print -rn -- "$1" >>| "$HISTFILE" 2>/dev/null || true
    return 0
  }

  add-zsh-hook zshaddhistory _mim_shell_state_append_zsh_history
  unset HISTSIZE SAVEHIST
fi
MIM_ZSHRC
}

prepare_zsh_shell_state() {
  shell_state_available || return 1
  export HISTFILE="${HISTFILE:-$shell_state_dir/.zsh_history}"
  write_zsh_state_files || return 1
  export ZDOTDIR="$mim_zdotdir"
  return 0
}

write_bash_rcfile() {
  mim_bashrc="$shell_state_dir/.mim-bashrc"

  cat > "$mim_bashrc" <<'MIM_BASHRC'
if [ -r "$HOME/.bash_profile" ]; then
  . "$HOME/.bash_profile"
elif [ -r "$HOME/.bash_login" ]; then
  . "$HOME/.bash_login"
elif [ -r "$HOME/.profile" ]; then
  . "$HOME/.profile"
fi

shopt -s histappend
HISTSIZE=
HISTFILESIZE=
export HISTSIZE HISTFILESIZE

_mim_shell_state_bash_history_append() {
  history -a
}

if declare -p PROMPT_COMMAND 2>/dev/null | grep -q '^declare -[^ ]*a'; then
  PROMPT_COMMAND+=(_mim_shell_state_bash_history_append)
elif [ -n "${PROMPT_COMMAND:-}" ]; then
  PROMPT_COMMAND="${PROMPT_COMMAND}; _mim_shell_state_bash_history_append"
else
  PROMPT_COMMAND="_mim_shell_state_bash_history_append"
fi
MIM_BASHRC
}

prepare_bash_shell_state() {
  shell_state_available || return 1
  export HISTFILE="${HISTFILE:-$shell_state_dir/.bash_history}"
  write_bash_rcfile || return 1
  export MIM_BASHRC="$mim_bashrc"
  return 0
}

launch_bash() {
  if prepare_bash_shell_state; then
    original_argc="$#"
    while [ "$original_argc" -gt 0 ]; do
      arg="$1"
      shift
      original_argc=$((original_argc - 1))
      case "$arg" in
        -l|--login) ;;
        *) set -- "$@" "$arg" ;;
      esac
    done

    if [ "$#" -eq 0 ]; then
      set -- -i
    fi

    exec "$shell" --rcfile "$MIM_BASHRC" "$@"
  fi

  if [ "$#" -eq 0 ]; then
    if [ "$auto_shell" = "1" ]; then
      exec "$shell" -l
    fi
    exec "$shell"
  fi
  exec "$shell" "$@"
}

auto_shell="0"
if [ "$#" -gt 0 ]; then
  shell="$1"
  shift
else
  auto_shell="1"
  resolve_auto_shell
fi

shell_name="$(basename "$shell")"
case "$shell_name" in
  zsh)
    prepare_zsh_shell_state || true
    if [ "$auto_shell" = "1" ] && [ "$#" -eq 0 ]; then
      set -- -l
    fi
    exec "$shell" "$@"
    ;;
  bash)
    launch_bash "$@"
    ;;
  *)
    exec "$shell" "$@"
    ;;
esac

# shellcheck shell=sh
# Installed as /etc/profile.d/agent-box.sh: login shells find the tools that
# Herdr Core, Claude Code and mise install under $HOME. Herdr clients start
# agents with `$SHELL -lic '...'`, and Debian's /etc/profile resets PATH before
# this runs. POSIX sh (any login shell sources it); prints nothing.

if [ -n "${HOME:-}" ]; then
    for agent_box_dir in "$HOME/.local/share/mise/shims" "$HOME/.local/bin"; do
        case ":${PATH:-}:" in
            *":$agent_box_dir:"*) ;;
            *) PATH="$agent_box_dir${PATH:+:$PATH}" ;;
        esac
    done
    unset agent_box_dir
    export PATH
fi

# Herdr derives its socket path from XDG_RUNTIME_DIR; the core runs without it.
unset XDG_RUNTIME_DIR

# agent-box

A container image for one long-lived "agent box": a pod where
[Herdr Core](https://github.com/herdrdev/herdr-core) keeps terminal sessions
alive and [Claude Code](https://code.claude.com/docs) runs inside them.
Clients reach it over SSH on port 2222: the Herdr desktop app, the Herdr phone
app, `hc` on another machine, or plain `ssh`. Herdr clients run
`herdr-core connect` on the box over that SSH connection.

Herdr Core and Claude Code both install under `$HOME` and update themselves in
place, so the home directory is a persistent volume. The image only supplies
the OS layer, an unprivileged sshd, a supervisor and a pinned seed copy of
herdr-core. Restarting the pod ends every running program, so the image is
built to make restarts unnecessary for anything but OS updates.

## Runtime contract

| What | Contract |
| --- | --- |
| User | uid/gid 1000 (`agent`), `allowPrivilegeEscalation: false`, all capabilities dropped. Nothing needs root. The root filesystem stays writable. |
| Volume | A persistent volume at `/home` (not `/home/agent`). With `fsGroup: 1000` its root is `root:1000`, mode `2775`. It may be empty on first start. |
| SSH keys | Optional: a Secret mounted as the directory `/etc/agent-box` with the key `authorized_keys` |
| Port | sshd on TCP 2222. Readiness: exec `/usr/local/bin/agent-box-ready` (see [Readiness](#readiness)). |
| Stop | preStop hook `/usr/local/bin/agent-box-stop`; it takes at most ~30 s |
| Environment | `TZ` is honoured. Nothing else is needed. |

Recommended in the pod spec: `fsGroupChangePolicy: OnRootMismatch` (the
entrypoint repairs the permission bits that an `Always` ownership pass breaks,
but the pass itself is wasted work), a fixed `hostname` (Herdr names the core
after the hostname it first starts on), and `terminationGracePeriodSeconds` of
45 or more.

Without a volume (`docker run`) the image's own `/home/agent` is used.

## What happens at start

`tini` is PID 1 and reaps the orphans Herdr leaves behind. Its child
`agent-box-entrypoint` does the following:

1. It creates `/home/agent` (mode 0750) if it is missing and copies missing
   files from `/etc/skel`. It creates `~/.ssh` and
   `~/.config/agent-box/services` (0700), `~/.local/bin` and `~/work`, and
   removes group/other write bits from `~`, `~/.ssh` and the host key. A
   Kubernetes fsGroup pass adds those bits, and sshd refuses them.
2. It copies the seed `/opt/agent-box/herdr-core` to `~/.local/bin/herdr-core`
   (plus an `hc` link), but only if no herdr-core is there yet.
3. It generates an ed25519 SSH host key, once, into
   `~/.config/agent-box/`, and prints its fingerprint to the log on every start.
4. It writes `~/.claude/settings.json` as `{"remoteControlAtStartup": true}`
   if that file does not exist.
5. It copies the managed keys (see below).
6. It starts Herdr Core (`hc server start`) in `~` with a clean environment,
   then the [services](#services), then sshd. The core starts first so that
   no client can start it.
7. If `~/.local/bin/claude` is missing, it runs the official Claude Code
   installer in the background, logging to
   `~/.local/state/agent-box/claude-install.log`. A failed install is retried
   on the next start.

After that the entrypoint supervises. It restarts sshd if sshd exits, and
every 30 s it keeps the services running and re-syncs the managed keys. Herdr
Core is never the main process, because it exits on `hc server restart`,
`hc update` and `hc machine update`. Herdr's holder process keeps the programs
running and starts a new core within about 3 s.

Herdr's own client calls also start a missing core. Over SSH, that core would
pick up the SSH session's login environment. To prevent that, the entrypoint
keeps a local connection to the core open. When the core exits, it brings a
new one back at once, before any client can redial. It also checks the core's
environment whenever the core's pid changes. If the environment is not its
own, the entrypoint restarts the core with `hc server restart --fresh`.
Programs keep running through that restart.

## The terminals' environment

The core's environment becomes the environment of every terminal it starts.
`agent-box-env` builds it from scratch:

- `HOME`, `USER`, `LOGNAME`
- `SHELL=/bin/bash`
- `LANG=C.UTF-8`
- `TZ`, if it is set
- `PATH`: `~/.local/bin`, then mise's shims, then the system directories

It contains no `SSH_*` variables, no Kubernetes variables and no secrets.
`XDG_RUNTIME_DIR` stays unset everywhere, because Herdr derives its socket path
from it. Without it, the socket is `~/.local/state/herdr-core/default/default.sock`.

Run `agent-box-env` with no arguments to print that environment. Put personal
variables in `~/.bashrc` (terminals run bash). Do not change the core's
environment: the entrypoint undoes changes to it.

Login shells get `~/.local/bin` and the mise shims on `PATH` from
`/etc/profile.d/agent-box.sh`, so `bash -lic` (how Herdr clients start agents)
finds `claude`, `hc` and `mise`. Non-interactive SSH sessions print nothing,
because Herdr speaks its protocol over their stdio.

## Updates

- **herdr-core**: Herdr's updaters (`hc update`, `hc machine update` from a
  client, the desktop app) replace `~/.local/bin/herdr-core` in place, and
  terminals keep running. There is exactly one herdr-core: never put another
  copy on the system `PATH`, or it would shadow those updates. The image seed
  only fills an empty volume, so changing `HC_VERSION` in the Dockerfile does
  not touch an existing home. To return to the seed, run
  `hc update --from /opt/agent-box/herdr-core` (add `--force` if it is older).
- **Claude Code** updates itself under `~/.local/share/claude`.
- **mise** is pinned by the image (`/usr/local/bin/mise`). Its tools, shims and
  config live under `$HOME`. `/etc/mise/config.toml` turns off its "new
  version" warning and its download statistics.
- **OS packages**: rebuild the image and roll the pod. The stop is clean:
  terminals started with `--keep` come back as shells in their last folder,
  the services start again, and all other programs end.

## SSH keys

- **Managed keys**: put public keys, one per line, in the Secret's
  `authorized_keys`. The entrypoint copies that file to
  `~/.ssh/authorized_keys.managed` (mode 0600). sshd's `StrictModes` rejects
  the Secret mount's own group/world-readable symlinks. The copy is refreshed
  within 30 s of the mounted file changing (kubelet itself can take a minute to
  update a mounted Secret). If the file disappears, the last copy stays.
- **Your own keys**: `~/.ssh/authorized_keys` is yours. The image creates it
  empty once and never writes it again (it only removes group/other permission
  bits).
- Only key logins are accepted: no passwords, no root. Agent forwarding is
  disabled, because every process in the box, Claude Code included, runs as the
  same user and could use a forwarded agent.
- On the first connection, compare the host key fingerprint with the line
  `SSH host key: ...` in the container log.

## Connecting

```sh
ssh -p 2222 agent@<box>                         # a shell; then `hc` to pick a terminal
hc machine add agent@<box>:2222 --name box      # from another machine with hc
hc run --on box --bg --name tests -- make test
```

In the Herdr apps, add the machine as `agent@<box>:2222`.

## Launching a session: agent-launch

`agent-launch` is the one command a phone shortcut runs over SSH. It starts
Claude Code on a prompt in a new background terminal, kept across restarts
(`hc run --keep`), and prints one line:

```console
$ ssh -p 2222 agent@<box> agent-launch "fix the flaky test in repo x"
started fix-the-flaky-test-1432 (t3) in /home/agent/work; attach: hc attach fix-the-flaky-test-1432
```

```text
agent-launch [-d DIR] [-n NAME] [--] PROMPT...
  -d DIR   working directory (default ~/work, created if missing)
  -n NAME  terminal name, 1-32 of a-z 0-9 - _ (default: first words + time)
```

All prompt words reach `claude` as a single argument. ssh joins its arguments
into one line that the remote shell splits again, so a prompt containing
quotes, `$` or backticks must be quoted for that remote shell. Easier: send the
prompt on stdin, where nothing rewrites it:

```sh
printf '%s' "$PROMPT" | ssh -p 2222 agent@<box> agent-launch -d ~/work/repo-x
```

## Services

A service is a long-running program that the supervisor keeps alive in a
named Herdr terminal. The main use is Claude Code's Remote Control server
(see [below](#remote-control)), which ends with every pod restart and after
about 10 minutes without network.

- A service is an executable regular file in `~/.config/agent-box/services/`
  (a script needs its `#!` line) whose name is 1-32 of `a-z 0-9 - _`,
  starting with a letter or digit. The file name is the terminal's name.
  Everything else in that directory is ignored: other names (`run.sh`),
  directories, symbolic links.
- Right after the container starts and then every 30 s, the supervisor starts
  each service that has no live terminal of its name, as
  `hc run --bg --name NAME -- ~/.config/agent-box/services/NAME`, in `~`,
  with [the terminals' environment](#the-terminals-environment). To watch or
  answer it: `hc attach NAME` (`ctrl+\` detaches).
- Service terminals are not kept (`--keep`): after a restart the supervisor
  starts the program again, where a kept terminal would come back as a shell
  holding the name.
- When a run ends, the supervisor logs its exit code (or why it ended), saves
  its last screen to `~/.local/state/agent-box/services/NAME.last.log` and
  removes the finished terminal, so finished terminals do not pile up under
  the name.
- A file without its exec bit is left alone: the supervisor neither starts
  it nor ends a terminal that still runs it.

The supervisor waits at least 60 s between two starts of a service. A run
that ends on its own within 120 s, or a start that fails, is a quick failure.
After 3 quick failures in a row the supervisor starts the service only every
15 minutes and logs a warning. A run that lasts more than 10 minutes clears
the count, and so do `agent-service enable` and `agent-service restart`. A
run ended by `hc kill` or by a stop of Herdr Core is not a failure. The
supervisor keeps these counts in memory, so they start over with the
container.

### agent-service

`agent-service` lists and switches services. It lives in `/usr/local/bin`,
so a one-line SSH command finds it:

```console
$ ssh -p 2222 agent@<box> agent-service ls
remote-control: enabled, live in t7 (pid 4211), last start 2026-11-26T09:12:40-0800
web: disabled, finished in t5 (ended by hc kill), last start 2026-11-26T08:58:10-0800
```

| Command | Effect |
| --- | --- |
| `agent-service ls` | Each service: enabled or disabled, its terminal (live with its pid, finished, or none), its last start, a pending backoff, and how its last run ended. Also lists the image's templates that are not installed and the files that are ignored. |
| `agent-service enable NAME` | Makes the file executable. If there is no such file, installs the image's template of that name (mode 0755). The supervisor starts it within 90 s. |
| `agent-service disable NAME` | Clears the exec bit, then ends the service's terminal (`hc kill`). It stays stopped. |
| `agent-service restart NAME` | Ends the service's terminal. The supervisor starts it again within 90 s. |

`enable` and `restart` also clear the quick-failure count. Errors exit
non-zero with a one-line message.

### Remote Control

The image ships one service template, not enabled:
`/usr/local/share/agent-box/services/remote-control`. It runs
`claude remote-control --spawn same-dir --no-create-session-in-dir` in
`~/work`. The box then appears as a device in the Claude app and on
claude.ai/code, sessions started from there work in `~/work`, and no session
exists until one is asked for. Remote Control needs a claude.ai
subscription.

1. Sign in to Claude Code once, in a shell on the box: `claude auth login`
   (or `claude`, then `/login`).
2. Enable the service: `agent-service enable remote-control`.
3. The first run asks once to enable Remote Control and to trust `~/work`:
   run `hc attach remote-control`, answer, and detach with `ctrl+\`.

Signed out, `claude remote-control` exits at once ("You must be logged in to
use Remote Control"), so a service enabled before signing in soon waits 15
minutes between tries. `agent-service restart remote-control` ends that
wait.

## Readiness

The pod's readiness probe runs `/usr/local/bin/agent-box-ready`. It exits 0
when sshd listens on TCP 2222 and no stop is in progress, and otherwise exits
1 with a one-line reason. A stop is in progress while agent-box-stop's mark
`/run/agent-box/stopping` is younger than 2 minutes, the same rule the
supervisor follows. The script reads the socket table (`ss`) instead of
connecting, because a TCP probe makes sshd log `Connection closed by ...` on
every check. It needs no capabilities and takes a few milliseconds.

## Stopping

`agent-box-stop` runs `hc server stop`, bounded to 25 s
(`AGENT_BOX_STOP_TIMEOUT`), and never fails. That is Herdr's only clean stop:
it parks the `--keep` terminals so that the next start brings them back as
shells in their last folder. A bare SIGTERM to the core is only a handover. The
pod's preStop hook calls it, and so does the entrypoint on SIGTERM before it
stops sshd and exits 0.

Before stopping the core, it makes sshd stop accepting new connections. A
connected Herdr client redials within a second, and its `herdr-core connect`
would start a new core. Open sessions stay, and readiness fails, as it should
for a pod that is going away.

For 2 minutes after that, the supervisor restarts neither the core nor sshd.
Run `agent-box-stop` by hand only when you mean that. Use `hc server restart`
to restart the core.

## Files

| File | Installed as |
| --- | --- |
| `Dockerfile` | the image, `linux/amd64`; built by `.github/workflows/agent-box.yaml` |
| `agent-box-entrypoint` | `/usr/local/bin/`: home setup and supervisor |
| `agent-box-env` | `/usr/local/bin/`: the core's (terminals') environment |
| `agent-box-ready` | `/usr/local/bin/`: readiness check (exec probe) |
| `agent-box-stop` | `/usr/local/bin/`: clean stop, preStop hook |
| `agent-launch` | `/usr/local/bin/`: start Claude Code on a prompt |
| `agent-service` | `/usr/local/bin/`: list and switch services |
| `services/remote-control` | `/usr/local/share/agent-box/services/`: service template |
| `sshd_config` | `/usr/local/etc/agent-box/sshd_config` |
| `profile.sh` | `/etc/profile.d/agent-box.sh` |
| `mise-system.toml` | `/etc/mise/config.toml` |

The image also must not contain `/etc/machine-id`. Herdr records it in its
state and, when it changes, takes a new identity and forgets its terminals. So
there is no systemd and no dbus in the image, and the build checks the file
after every apt step.

## Troubleshooting

- Container log: supervisor, sshd and agent-box-stop messages, including from
  the preStop hook.
- `~/.local/state/herdr-core/default/core.log` and `holder.log`: Herdr itself.
- `~/.local/state/agent-box/claude-install.log`: the Claude Code installer.
- `agent-service ls`, and `~/.local/state/agent-box/services/NAME.last.log`:
  how a service's last run ended, with its last screen.
- A key is refused: sshd logs why. Check the modes of `~`, `~/.ssh` and the key
  files; none may be group- or world-writable.

## Running locally

```sh
docker build -t agent-box containers/agent-box
docker volume create agent-box-home
docker run --rm --user 0 --entrypoint /bin/sh -v agent-box-home:/mnt agent-box \
  -c 'chown 0:1000 /mnt && chmod 2775 /mnt'    # what fsGroup does in the pod
docker run -d --name agent-box -p 2222:2222 \
  --cap-drop ALL --security-opt no-new-privileges \
  -v agent-box-home:/home:nocopy \
  -v "$PWD/keys:/etc/agent-box:ro" \
  agent-box
```

`nocopy` keeps Docker from pre-filling the empty volume with the image's
`/home`, which matches a fresh PVC. `keys/authorized_keys` holds your public
key.

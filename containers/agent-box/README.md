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
| Port | sshd on TCP 2222. Readiness: a TCP check on 2222. |
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
   files from `/etc/skel`. It creates `~/.ssh` (0700), `~/.local/bin` and
   `~/work`, and removes group/other write bits from `~`, `~/.ssh` and the
   host key. A Kubernetes fsGroup pass adds those bits, and sshd refuses them.
2. It copies the seed `/opt/agent-box/herdr-core` to `~/.local/bin/herdr-core`
   (plus an `hc` link), but only if no herdr-core is there yet.
3. It generates an ed25519 SSH host key, once, into
   `~/.config/agent-box/`, and prints its fingerprint to the log on every start.
4. It writes `~/.claude/settings.json` as `{"remoteControlAtStartup": true}`
   if that file does not exist.
5. It copies the managed keys (see below).
6. It starts Herdr Core (`hc server start`) in `~` with a clean environment,
   then starts sshd. The core starts first so that no client can start it.
7. If `~/.local/bin/claude` is missing, it runs the official Claude Code
   installer in the background, logging to
   `~/.local/state/agent-box/claude-install.log`. A failed install is retried
   on the next start.

After that the entrypoint supervises. It restarts sshd if sshd exits, and
re-syncs the managed keys every 30 s. Herdr Core is never the main process,
because it exits on `hc server restart`, `hc update` and `hc machine update`.
Herdr's holder process keeps the programs running and starts a new core within
about 3 s.

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
  and all other programs end.

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
| `agent-box-stop` | `/usr/local/bin/`: clean stop, preStop hook |
| `agent-launch` | `/usr/local/bin/`: start Claude Code on a prompt |
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

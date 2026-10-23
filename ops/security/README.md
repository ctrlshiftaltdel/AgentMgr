# Managed execution deployment templates (#82)

This directory contains the **production** templates for the managed execution
substrate. They are templates, not an installer, and nothing here was installed
or exercised with root privileges in Issue #82.

> **Scope.** The #82 live isolation tests use the controller-side
> `SudoSystemdOps` adapter against an approved test host. They do **not**
> install this helper, add a sudoers rule, or create a persistent user. The
> helper in this directory is the production path and is unit-tested locally
> without root.

## Files

| File | Installed as | Purpose |
| --- | --- | --- |
| `agentmgr_exec.py` | `/usr/local/libexec/agentmgr_exec.py` | Root-owned narrow helper (Python 3 standard library only). |
| `agentmgr-exec` | `/usr/local/libexec/agentmgr-exec` | Thin `#!/bin/sh` wrapper that execs the helper. |
| `agentmgr-exec.sudoers` | `/etc/sudoers.d/agentmgr-exec` | One fixed executable for the controller user. |
| `agentmgr-pep-gateway.service` | `/etc/systemd/system/` | Production gateway service template (#83). |
| `agentmgr-task@.service` | `/etc/systemd/system/` | Documented sandbox template; transient units remain the default. |

## Install

`<controller-user>` and `<controller-group>` are placeholders for the
dedicated controller/gateway service account. `agentmgr` in the sudoers
template is that same placeholder, not a repository account. No persistent
user is created by these templates; create the service account with the
platform's normal account tooling if the deployment does not already have one.

1. Create the runtime root owned by the controller user:

   ```sh
   sudo install -d -o <controller-user> -g <controller-group> -m 0700 /run/agentmgr-exec
   ```

   The helper creates `credentials/` and `specs/` inside it as `root:root
   0700`. Do not pre-create those two directories with broader permissions:
   the helper refuses a directory it does not own or that is not `0700`.

2. Install the helper, wrapper and operator README as root-owned files:

   ```sh
   sudo install -o root -g root -m 0644 ops/security/agentmgr_exec.py \
       /usr/local/libexec/agentmgr_exec.py
   sudo install -o root -g root -m 0755 ops/security/agentmgr-exec \
       /usr/local/libexec/agentmgr-exec
   sudo install -d -o root -g root -m 0755 /usr/local/share/doc/agentmgr/security
   sudo install -o root -g root -m 0644 ops/security/README.md \
       /usr/local/share/doc/agentmgr/security/README.md
   ```

3. Install the sudoers rule, replacing the placeholder with the controller
   user, then validate it before it is used:

   ```sh
   sudo install -o root -g root -m 0440 ops/security/agentmgr-exec.sudoers \
       /etc/sudoers.d/agentmgr-exec
   sudo visudo -c
   ```

   The rule fixes one executable. It deliberately does not constrain
   arguments and grants no broad unit or command wildcard; argument
   validation lives in the helper.

4. Deploy the fixed workload and gateway socket under `/run/agentmgr-exec`:

   - `workload.py` is a regular file owned by root, mode `0555`;
   - `gateway.sock` is the gateway's Unix socket, owned by the controller
     user;
   - both paths are fixed in the helper and bind-mounted into the task as
     `/workload.py` and `/run/agentmgr/gateway.sock`.

5. Install a service template only if the deployment needs a persistent unit
   definition. Replace `<controller-user>`, `<controller-group>` and
   `<install-root>` in `agentmgr-pep-gateway.service` first:

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable --now agentmgr-pep-gateway.service
   ```

## Ownership and permissions

| Path | Owner / mode | Notes |
| --- | --- | --- |
| `/usr/local/libexec/agentmgr_exec.py` | `root:root 0644` | Standard library only; never imports `app.security`. |
| `/usr/local/libexec/agentmgr-exec` | `root:root 0755` | Fixed wrapper path referenced by sudoers. |
| `/etc/sudoers.d/agentmgr-exec` | `root:root 0440` | Validated with `visudo -c`. |
| `/run/agentmgr-exec` | `<controller-user> 0700` | Gateway socket and revocation records live here. |
| `/run/agentmgr-exec/credentials` | `root:root 0700` | Created by the helper; files are `0600`. |
| `/run/agentmgr-exec/specs` | `root:root 0700` | Created by the helper; files are `0600`. |
| `/run/agentmgr-exec/workload.py` | `root:root 0555` | Read-only bind mount. |
| `/var/lib/private/agentmgr-exec` | `root:root` | Per-unit state directories, owned by dynamic UIDs. |
| `/var/lib/agentmgr-exec` | `root:root` | Compatibility symlinks created by systemd. |

## Request protocol

The controller writes **one JSON object to stdin**. The helper accepts no
arguments: `agentmgr-exec` forwards `"$@"`, and any non-empty argv is rejected
before a request is parsed. Nothing secret is ever placed in argv, the
environment or a log.

```json
{"command": "launch", "spec": {"...": "..."}, "credential": "agx1...."}
```

| `command` | Request keys | Behaviour |
| --- | --- | --- |
| `launch` | `command`, `spec`, `credential` | Validate the exact spec field set, write root-only `specs/<unit>.json` and `credentials/<unit>.json` with `O_CREAT|O_EXCL|O_NOFOLLOW` and `0600`, then run the fixed `systemd-run` argv. |
| `stop` | `command`, `unit` | Fixed `systemctl stop <unit>`. |
| `terminate` | `command`, `unit` | Fixed `systemctl kill --signal=SIGTERM --kill-whom=all <unit>`; keeps exit evidence available. |
| `kill` | `command`, `unit` | Fixed `systemctl kill --signal=SIGKILL --kill-whom=all <unit>`. |
| `inspect` | `command`, `unit` | Fixed `systemctl show` property projection; returns `{"show": {<property>: <value>}}`. |
| `reset-failed` | `command`, `unit` | Fixed `systemctl reset-failed <unit>`; `LoadState=not-found` is a no-op. |
| `remove-sources` | `command`, `unit` | Deletes only `credentials/<unit>.json` and `specs/<unit>.json`. |
| `cleanup` | `command`, `unit` | `reset-failed`, then deletes only the unit's derived source files, state directory and compatibility symlink. |

Transient units are launched with `RemainAfterExit=yes`, so a successfully
exited unit remains loaded as `active/exited` until the supervisor reads
`ExecMainExitTimestamp`/`Result`; failed/timeout units stay `failed` under
systemd's default GC. `ExitType=cgroup` makes `MainPID=0`/`active-exited` mean
the whole cgroup is empty. `CollectMode` is left at its default (`inactive`);
it is not a retention mechanism and inactive units are still unloaded. The
supervisor requests SIGTERM with `terminate`, reads the exit evidence, and
leaves collection to `cleanup` (whose `stop` handles a loaded `active/exited`
unit); `reset-failed` on a collected (`LoadState=not-found`) unit is a no-op.

Unit names must match `^agentmgr-task-[0-9a-f]{12}$`. The unit is re-derived
from the validated task ID, so a client cannot select an arbitrary unit. The
helper rejects unknown top-level fields and unknown spec fields; systemd
properties such as `ExecStart`, `User`, `Environment` and `BindPaths` cannot
be expressed. Child commands run from absolute paths as argv arrays with
`shell=False` and a fixed minimal environment (`env -i` equivalent).

On failure the helper writes `{"error": "<stable_code>"}` to stderr and exits
non-zero. It never prints a path, token or traceback. On success it writes one
JSON object to stdout.

## Tests

The helper and templates are covered by local unit tests that require no
root, sudo or systemd:

```sh
uv run --no-sync pytest app/tests/security/test_helper.py -q
```

The tests load `ops/security/agentmgr_exec.py` from the repository, use a
temporary runtime root and a recording runner, and compare the duplicated
sandbox properties with `app.security.systemd.build_systemd_run_argv` so the
template cannot silently drift from the product builder.

The live #82 harness (`app/tests/security/test_isolation_live.py`,
`ops/security/verify_isolation.sh`) uses `SudoSystemdOps` and does not install
this helper.

## Rollback

1. Stop new task creation and the controller's monitoring loop.
2. Stop and disable the gateway unit if it was installed:
   `sudo systemctl disable --now agentmgr-pep-gateway.service`.
3. Remove the sudoers rule and re-validate:
   `sudo rm /etc/sudoers.d/agentmgr-exec && sudo visudo -c`.
4. Remove the helper, wrapper and service templates:
   `sudo rm /usr/local/libexec/agentmgr-exec /usr/local/libexec/agentmgr_exec.py`
   and the unit files under `/etc/systemd/system`, then run
   `sudo systemctl daemon-reload`.
5. After confirming that no `agentmgr-task-*` unit remains, remove
   `/run/agentmgr-exec`, `/var/lib/private/agentmgr-exec`,
   `/var/lib/agentmgr-exec` and `/srv/agentmgr-exec-resources`.

There is no database migration to roll back. `DynamicUser` UIDs are reclaimed
by systemd; no persistent user was created. Existing audit and evidence files
are retained under the deployment's normal retention policy.

## Limitations and #83 boundary

- The helper hardcodes the deployment paths in design §7.3. A deployment that
  uses different runtime/state roots must change the template and the
  controller configuration together; the production `HelperSystemdOps` path
  always launches the `private_netns` profile.
- `agentmgr-pep-gateway.service` is a **#83** template. Issue #82's live
  harness runs the gateway in-process with an in-memory live
  `GrantStatusProvider`; it does not start this unit, does not write a
  `gateway.pid`, and does not claim a production gateway was verified.
- Not covered here: a real Grant store adapter, file-backed cross-process
  revocation, real business resource backends, model-provider egress,
  browser/terminal governance, multi-host or multi-tenant scheduling, and
  image/UI assets. Those remain deferred and are not represented as enforced.
- Root or controller sudo can bypass every sandbox property; systemd/kernel
  defects, side channels and physical access remain outside the boundary. The
  authoritative risk list is `docs/security/managed-execution-isolation-design.md`
  §15.2–§15.3 and the deployment boundary record.
- No live or root-privileged validation was performed while writing these
  templates. Do not treat them as verified until a deployment records real
  command output and exit codes.

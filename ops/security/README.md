# Managed execution deployment templates (#82)

This directory contains the **production** templates for the managed execution
substrate. They are templates, not an installer, and nothing here was installed
or exercised with root privileges in Issue #82.

> **Scope.** The #82 live isolation tests use the controller-side
> `SudoSystemdOps` adapter against an approved test host. They do **not**
> install this helper, add a sudoers rule, or create a persistent user. The
> helper in this directory is the production path and is unit-tested locally
> without root.
>
> **Two families.** This README documents #82's `agentmgr-exec` family below.
> Issue #97 adds a separate Hermes sandbox family (`agentmgr_hermes_*`,
> `agentmgr-hermes-*`) with its own installer and verifier; see
> [Hermes managed execution (#97)](#hermes-managed-execution-97) at the end.
> The two families share no helper, sudoers rule, runtime root or unit names.

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

5. `agentmgr-pep-gateway.service` is a **#83 placeholder**, not a working
   production unit. Replace `<controller-user>`, `<controller-group>` and
   `<install-root>` first, but do **not** enable it unconditionally:

   ```sh
   sudo systemctl daemon-reload
   ```

   The template is gated by `ConditionPathExists=/etc/agentmgr/pep-gateway.enabled`
   and sets `Restart=no`. `python -m app.security.gateway` currently exits `2`
   with a stable message and **does not bind a socket**, because no production
   `GrantStatusProvider` adapter exists yet (#83). Enable the unit only after
   #83 supplies that adapter, the adapter has been reviewed, and the operator
   has created the enable marker:

   ```sh
   # #83 only, after the live Grant adapter is installed and reviewed:
   sudo install -o root -g root -m 0600 /dev/null /etc/agentmgr/pep-gateway.enabled
   sudo systemctl enable --now agentmgr-pep-gateway.service
   ```

   The unit deliberately has no ordering dependency on the transient
   `agentmgr-task@` units: the controller starts those on demand, so they are
   not ordered against the gateway.

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
| `inspect` | `command`, `unit` | Fixed `systemctl show --timestamp=us` property projection; returns `{"show": {<property>: <value>}}`. |
| `reset-failed` | `command`, `unit` | Fixed `systemctl reset-failed <unit>`; `LoadState=not-found` is a no-op. |
| `remove-sources` | `command`, `unit` | Deletes only `credentials/<unit>.json` and `specs/<unit>.json`. |
| `cleanup` | `command`, `unit` | Idempotent `systemctl stop`, `reset-failed`, then deletes only the unit's derived source files, state directory and compatibility symlink. |

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
  `gateway.pid`, and does not claim a production gateway was verified. The
  standalone `python -m app.security.gateway` entry point fails closed with
  exit code `2` until #83 supplies a production Grant adapter; it never binds a
  socket today.
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

## Hermes managed execution (#97)

Issue #97 adds a **separate** Hermes-in-sandbox family. It does not share the
#82 helper, sudoers rule, runtime root or unit names, and it never starts or
mounts a personal/default Hermes gateway or profile. Like the #82 templates,
nothing here was installed or exercised with root privileges while the changes
were written; the installer and verifier were syntax/static checked only.

| File | Installed as | Purpose |
| --- | --- | --- |
| `agentmgr_hermes_exec.py` | `/usr/local/libexec/agentmgr_hermes_exec.py` | Root helper: fixed `systemd-run` argv, stdin-JSON request, launch/status/stop/cleanup/attest. |
| `agentmgr-hermes-exec` | `/usr/local/libexec/agentmgr-hermes-exec` | Fixed `#!/bin/sh` wrapper with no arguments. |
| `agentmgr_hermes_workload.py` | `/usr/local/libexec/agentmgr_hermes_workload.py` | Unit workload: local loopback model proxy + one `hermes` one-shot process. |
| `agentmgr-hermes.sudoers` | `/etc/sudoers.d/agentmgr-hermes` | One argument-less wrapper command for the adapter account. |
| `agentmgr-hermes-egress.service` | `/etc/systemd/system/agentmgr-hermes-egress.service` | Host egress broker (the only holder of the provider key). |
| `provision_hermes_runtime.sh` | (not installed) | Idempotent root installer: groups/users, read-only runtime root + manifest hash, configs, keys, sudoers, unit. |
| `verify_hermes_isolation.sh` / `.py` | (not installed) | Read-only post-install verification; no task unit required. |

### Install

```sh
sudo ops/security/provision_hermes_runtime.sh --dry-run \
  --adapter-user <adapter-user> --install-root <install-root> \
  --venv <venv-bin-dir> --service-identity-id svc_<id> \
  --source <runtime-source> --runtime-version <version> \
  --model <model-name> --provider-host <provider-host> \
  --provider-base-url https://<provider-host> \
  --provider-key-file <root-only-provider-key> \
  --evidence-ref evidence:hermes:managed:<id>
# review the dry-run output, then rerun without --dry-run
```

The script:

- creates the `agentmgr-hermes` sandbox group, the `agentmgr-hermes-adapter`
  private group and the dedicated broker user; the adapter user must already
  exist and is added to both groups;
- creates the task/control/revocation directories with the exact group and mode
  the helper and broker expect;
- prepares `/var/lib/agentmgr-hermes-runtime` from `--source` (`copy` default or
  a read-only `bind`) and records a deterministic `sha256:` manifest hash; it
  refuses a source with `profiles`, `auth.json`, `.env`, `sessions`,
  `gateway.lock` or `locks` at **any depth**, any absolute symlink or relative
  symlink escaping the root, and home/repository/personal-profile paths;
- refuses symlinked or non-directory fixed paths (repairing owner/group/mode on
  a real directory) and symlinked install targets; never copies through a
  pre-existing runtime-root symlink or mount;
- requires the `https://` provider URL host/port to exactly equal
  `--provider-host`, because the broker injects the real key into that URL;
- keeps existing HMAC keys and logs that rotation is an explicit operator
  action; refuses to replace a different existing provider key;
- installs the helper, wrapper, workload, sudoers rule, broker unit, helper
  install config, launcher config, broker config and deployment record;
- generates the attestation and egress-token HMAC keys (root:adapter-private
  `0440`, never sandbox-group readable), copies the operator-supplied provider
  key as root-only `0400` and writes the provisioning runtime-attestation
  record; no secret is printed;
- **does not start any unit** and never touches a personal/default Hermes
  gateway or profile.

After install:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now agentmgr-hermes-egress.service
sudo ops/security/verify_hermes_isolation.sh
```

`verify_hermes_isolation.sh` (root, read-only) checks installed hashes and
modes, the exact sudoers rule, group memberships, the runtime manifest hash
against the install/launcher/broker configs, the task/control/revocation
directories, the broker socket, key permissions/ownership, the absence of
personal profiles/bind mounts, the provider URL host/port against the claimed
provider host, and the installed helper's full normalized `systemd-run` argv
(including every `InaccessiblePaths`/`BindPaths`/`BindReadOnlyPaths` property
and the fixed workload argv). It exits non-zero on any mismatch and starts no
task unit.

### Rollback

```sh
sudo ops/security/provision_hermes_runtime.sh --rollback
# optional, after confirming the accounts are no longer used:
sudo ops/security/provision_hermes_runtime.sh --rollback --purge-groups
```

This stops every `agentmgr-hermes-*` transient unit, disables the broker,
removes the unit/sudoers/helper/wrapper/workload and `/etc/agentmgr/hermes/**`,
unmounts a bind runtime root and removes the runtime/task/control/revocation
directories. There is no database migration and no persistent task state to
roll back. The personal/default Hermes gateway and profiles are not touched.

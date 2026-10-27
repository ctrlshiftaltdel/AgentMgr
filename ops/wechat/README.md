# WeChat B1 ops assets

- `ajaimgr-wechat-bridge.service` — systemd **template** for the local bridge.
  Replace every `<placeholder>`; do not install or start it from a code change.
- `wechat-bridge.env.example` — environment-file example with no secrets.
- Full runbook: [`docs/operations/wechat-bridge.md`](../../docs/operations/wechat-bridge.md).

B1 is a **local trusted service**: it opens no public port and has no token/HTTP
surface. The internal token/network path is deferred until a later increment.

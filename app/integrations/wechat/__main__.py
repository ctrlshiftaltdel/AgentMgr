"""``python -m app.integrations.wechat`` entry point."""

from __future__ import annotations

from app.integrations.wechat.run import main

if __name__ == "__main__":
    raise SystemExit(main())

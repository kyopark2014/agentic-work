#!/bin/sh
set -e

if [ -n "$APP_CONFIG_JSON" ]; then
  echo "$APP_CONFIG_JSON" > /app/application/config.json
fi

# Merge ECS-injected secrets into config.json for modules that only read config.
if [ -f /app/application/config.json ]; then
  python3 - <<'PY'
import json
import os

path = "/app/application/config.json"
with open(path, "r", encoding="utf-8") as f:
    cfg = json.load(f)
if not isinstance(cfg, dict):
    raise SystemExit(0)

changed = False
gw = (os.environ.get("LLM_GATEWAY_KEY") or "").strip()
if gw and cfg.get("llm_gateway_key") != gw:
    cfg["llm_gateway_key"] = gw
    changed = True

if changed:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
PY
fi

exec "$@"

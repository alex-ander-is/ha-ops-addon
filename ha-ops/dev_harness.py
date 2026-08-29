#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import dev_harness


def main():
    parser = argparse.ArgumentParser(description="Run HA Ops against a safe local dev harness.")
    parser.add_argument("--root", help="Disposable root. Explicit roots must be under ha-ops/.ha-ops-dev or a temp dir.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--keep-root", action="store_true")
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args()

    ctx = dev_harness.create_context(root=args.root, host=args.host, port=args.port, keep_root=args.keep_root)
    httpd = dev_harness.serve_context(ctx)
    payload = {
        "baseUrl": dev_harness.ingress_base_url(ctx),
        "root": str(ctx.dev_harness_root),
        "diagnosticsUrl": dev_harness.ingress_base_url(ctx) + "__dev_harness__/diagnostics",
        "host": ctx.host,
        "port": ctx.port,
    }
    if args.print_json:
        print(json.dumps(payload, sort_keys=True), flush=True)
    else:
        print(f"HA Ops dev harness: {payload['baseUrl']}", flush=True)
        print(f"root: {payload['root']}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        dev_harness.cleanup_context(ctx)


if __name__ == "__main__":
    main()

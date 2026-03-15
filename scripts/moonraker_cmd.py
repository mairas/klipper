#!/usr/bin/env python3
"""Send G-code to a Klipper printer via Moonraker and poll for completion.

Sends the command in a background thread (Moonraker blocks until done),
while the main thread polls /server/gcode_store for new output.

Usage:
    moonraker_cmd.py [--host HOST] [--timeout SECS] [--poll SECS] GCODE...

Examples:
    moonraker_cmd.py G28
    moonraker_cmd.py --timeout 120 G28
    moonraker_cmd.py "PROBE_SCAN_TEST X=30 Y=20 LENGTH=50"
    moonraker_cmd.py --host 10.84.77.78 G28
"""

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "ratos.hal"
DEFAULT_TIMEOUT = 90
DEFAULT_POLL_INTERVAL = 1.0


def api_get(host, path):
    url = f"http://{host}{path}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def api_post(host, path, data, timeout=300):
    url = f"http://{host}{path}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def get_gcode_store(host, count=200):
    result = api_get(host, f"/server/gcode_store?count={count}")
    return result["result"]["gcode_store"]


def send_gcode(host, gcode):
    """Send gcode; blocks until Moonraker returns."""
    return api_post(host, "/printer/gcode/script", {"script": gcode})


def run(host, gcode, timeout, poll_interval):
    # Snapshot gcode store timestamp before sending
    pre_store = get_gcode_store(host)
    pre_time = pre_store[-1]["time"] if pre_store else 0.0

    # Send command in background thread (Moonraker blocks until complete)
    result = {"done": False, "error": None}

    def _send():
        try:
            send_gcode(host, gcode)
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err = json.loads(body)
                result["error"] = err["error"]["message"]
            except (json.JSONDecodeError, KeyError):
                result["error"] = f"HTTP {e.code}: {body}"
        except Exception as e:
            result["error"] = str(e)
        finally:
            result["done"] = True

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()

    print(f">>> {gcode}", file=sys.stderr)

    # Poll for new output until command thread finishes or timeout
    deadline = time.monotonic() + timeout
    last_printed_time = pre_time

    while time.monotonic() < deadline:
        time.sleep(poll_interval)

        # Print any new gcode responses
        try:
            store = get_gcode_store(host)
        except Exception:
            continue
        new_msgs = [m for m in store if m["time"] > last_printed_time]
        for msg in new_msgs:
            prefix = "!! " if msg["type"] == "command" else ""
            print(f"{prefix}{msg['message']}")
            last_printed_time = msg["time"]

        if result["done"]:
            # Drain final messages
            time.sleep(0.5)
            try:
                store = get_gcode_store(host)
                final = [m for m in store if m["time"] > last_printed_time]
                for msg in final:
                    prefix = "!! " if msg["type"] == "command" else ""
                    print(f"{prefix}{msg['message']}")
            except Exception:
                pass

            if result["error"]:
                print(f"ERROR: {result['error']}", file=sys.stderr)
                return 1
            return 0

    print(f"TIMEOUT after {timeout}s", file=sys.stderr)
    return 2


def main():
    parser = argparse.ArgumentParser(
        description="Send G-code to Klipper via Moonraker and wait for output")
    parser.add_argument("gcode", nargs="+", help="G-code command(s) to send")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Moonraker host (default: {DEFAULT_HOST})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"Max seconds to wait (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_INTERVAL,
                        help=f"Poll interval in seconds (default: {DEFAULT_POLL_INTERVAL})")
    args = parser.parse_args()

    gcode = " ".join(args.gcode)
    sys.exit(run(args.host, gcode, args.timeout, args.poll))


if __name__ == "__main__":
    main()

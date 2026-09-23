"""
server.py
=========
Thin web wrapper around scheduler.py, so the Layer1/Layer2 pipeline can run
as a Render "Web Service" (which requires binding to $PORT) instead of a
separate "Background Worker" service.

On import (i.e. as soon as gunicorn loads this module):
  1. Wires up Render's persistent Disk (if mounted) so layer1/output and
     layer2/output survive redeploys/restarts — see _wire_persistent_disk().
  2. Starts a background thread that calls scheduler.run_tick() on a loop,
     exactly like `scheduler.py --loop` — every LOOP_CHECK_INTERVAL_SECONDS,
     self-gated by publish_frequency_hours / empty_batch_retry_hours as usual.

Endpoints:
  GET  /healthz          — plain 200 OK, wired up as Render's health check
  GET  /status           — same info as `scheduler.py --status`, as JSON
  POST /trigger?force=true — manually run a tick in the background.
                             Requires header `X-Admin-Token: <token>`
                             matching the ADMIN_TRIGGER_TOKEN env var,
                             since this URL is publicly reachable.

IMPORTANT — run with a single worker:
  gunicorn server:app --workers 1 ...
Two workers would each start their own background scheduler thread and
publish products twice. --workers 1 --threads N (for concurrent HTTP
requests) is correct here; do not scale workers without also moving the
scheduler loop out of this process (e.g. to a separate Background Worker
or Cron Job service).
"""

import os
import threading
import time
import traceback
from pathlib import Path

from flask import Flask, jsonify, request

import scheduler  # reused as-is — same run_tick/state logic as `scheduler.py --loop`

app = Flask(__name__)

LOOP_CHECK_INTERVAL_SECONDS = scheduler.LOOP_CHECK_INTERVAL_SECONDS
ADMIN_TRIGGER_TOKEN = os.getenv("ADMIN_TRIGGER_TOKEN", "")

# One-off safety valve: on a fresh deploy, the background thread's FIRST
# tick fires within a second or two of startup — too fast to SSH in and
# migrate seen_pids.json/published_pids.json onto the volume first. Set
# this env var (seconds) to delay that first tick, giving yourself a window
# to upload state before Layer 1 can possibly run. Safe to leave at 0 (no
# delay) once the volume already has real state on it.
STARTUP_DELAY_SECONDS = int(os.getenv("SCHEDULER_INITIAL_DELAY_SECONDS", "0"))

# Prevents the background loop's tick and a manually-triggered /trigger tick
# from ever running at the same time and racing on publish_state.json.
_tick_lock = threading.Lock()


# ── Persistent disk wiring ───────────────────────────────────────────────────

def _wire_persistent_disk() -> None:
    """
    If a persistent volume/disk is mounted (PERSISTENT_DISK_PATH — e.g. a
    Railway Volume's mount path, or RENDER_DISK_PATH for backward
    compatibility if you deployed this on Render before), symlink
    layer1/output and layer2/output into it. Without this, seen_pids.json,
    winning_products.json, publish_state.json, and the published-products
    registry all live on the service's local (ephemeral) filesystem and are
    WIPED on every redeploy — which would cause duplicate publishing and
    repeated Layer 1 discovery of the same products.

    Falls back to plain local directories (with a warning) if neither env
    var is set — fine for local dev, not for production.
    """
    disk_path = os.getenv("PERSISTENT_DISK_PATH") or os.getenv("RENDER_DISK_PATH")
    if not disk_path:
        print("[Server] ⚠️  PERSISTENT_DISK_PATH not set — using EPHEMERAL local storage. "
              "This is fine for local testing but will lose seen_pids/publish_state/"
              "winning_products on every redeploy. Set PERSISTENT_DISK_PATH + attach a "
              "persistent volume (e.g. a Railway Volume) for production.")
        return

    disk_root = Path(disk_path)
    mappings = {
        Path("layer1/output"): disk_root / "layer1_output",
        Path("layer2/output"): disk_root / "layer2_output",
    }

    for local_path, target in mappings.items():
        target.mkdir(parents=True, exist_ok=True)

        if local_path.is_symlink():
            continue  # already wired up from a previous startup

        if local_path.exists():
            print(f"[Server] ⚠️  {local_path} already exists as a real directory — "
                  f"leaving it as-is (NOT symlinked to the persistent disk). Delete it "
                  f"manually (once) if you want it backed by the persistent disk instead.")
            continue

        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            local_path.symlink_to(target, target_is_directory=True)
            print(f"[Server] ✅ Symlinked {local_path} -> {target}")
        except OSError as e:
            print(f"[Server] ⚠️  Could not symlink {local_path} -> {target}: {e}")


# ── Background scheduler loop ────────────────────────────────────────────────

def _background_loop() -> None:
    if STARTUP_DELAY_SECONDS > 0:
        print(f"[Server] ⏸️  Delaying first scheduler tick by {STARTUP_DELAY_SECONDS}s "
              f"(SCHEDULER_INITIAL_DELAY_SECONDS) — migrate state files onto the volume "
              f"via `railway ssh` now, before Layer 1 can run.")
        time.sleep(STARTUP_DELAY_SECONDS)
    print(f"[Server] 🔁 Background scheduler loop started "
          f"(checking every {LOOP_CHECK_INTERVAL_SECONDS}s)...")
    while True:
        try:
            with _tick_lock:
                scheduler.run_tick(force=False)
        except Exception as e:
            print(f"[Server] ❌ Background tick failed: {e}")
            traceback.print_exc()
        time.sleep(LOOP_CHECK_INTERVAL_SECONDS)


_background_thread_started = False


def _start_background_thread_once() -> None:
    global _background_thread_started
    if _background_thread_started:
        return
    _background_thread_started = True
    thread = threading.Thread(target=_background_loop, daemon=True)
    thread.start()


# Runs at import time (i.e. when gunicorn loads "server:app"), not just
# under `if __name__ == "__main__"` — gunicorn never executes that block.
_wire_persistent_disk()
_start_background_thread_once()


# ── HTTP endpoints ────────────────────────────────────────────────────────────

@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"}), 200


@app.route("/status", methods=["GET"])
def status():
    layer2_cfg = scheduler.get_layer_config("layer2")
    batch_size = layer2_cfg.get("products_per_publish_run", 5)
    frequency_hours = layer2_cfg.get("publish_frequency_hours", 72)
    empty_retry_hours = layer2_cfg.get("empty_batch_retry_hours", 6)
    state = scheduler._load_state()

    payload = {
        "products_per_publish_run": batch_size,
        "publish_frequency_hours": frequency_hours,
        "empty_batch_retry_hours": empty_retry_hours,
        "state": state or None,
    }
    if state and state.get("last_run_at"):
        next_due = scheduler._next_due_at(state, frequency_hours, empty_retry_hours)
        payload["next_check_due_at"] = next_due.isoformat()
    return jsonify(payload), 200


@app.route("/trigger", methods=["POST"])
def trigger():
    token = request.headers.get("X-Admin-Token", "")
    if not ADMIN_TRIGGER_TOKEN or token != ADMIN_TRIGGER_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    force = request.args.get("force", "false").lower() == "true"

    def _run():
        with _tick_lock:
            scheduler.run_tick(force=force)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "triggered", "force": force}), 202


if __name__ == "__main__":
    # Local dev only — Render uses gunicorn (see render.yaml's startCommand).
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
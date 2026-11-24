"""Long-running add-on service: periodic optimisation plus read-only web UI."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time
from pathlib import Path

from .cli import build_parser, run_once
from .config import ConfigError
from .ha_client import HomeAssistantClient, HomeAssistantError
from .observability import Metrics
from .webui import serve

LOGGER = logging.getLogger("voltwise.service")
_STOP = threading.Event()


def _handle_signal(signum, frame) -> None:  # pragma: no cover - signal path
    LOGGER.info("shutdown signal received")
    _STOP.set()


def build_service_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="voltwise-service")
    parser.add_argument("--config", default="/data/options.json")
    parser.add_argument("--output", default="/data/voltwise/plan.json")
    parser.add_argument("--web-port", type=int, default=8099)
    parser.add_argument("--web-host", default=None,
                        help="bind address (default: VOLTWISE_WEB_HOST or 0.0.0.0 for ingress)")
    parser.add_argument("--interval-seconds", type=int, default=None,
                        help="override the cycle interval (default: plan interval_minutes)")
    parser.add_argument("--apply", action="store_true", help="allow writes when enabled in config")
    parser.add_argument("--confirm", action="store_true", help="confirm actuation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_service_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .cli import load_config
    from .config import VoltWiseConfig

    try:
        plan_config = VoltWiseConfig.from_dict(load_config(args.config))
    except (ConfigError, ValueError, OverflowError, RecursionError, OSError,
            HomeAssistantError) as exc:
        LOGGER.error("configuration error: %s: %s", type(exc).__name__, exc)
        return 2
    if args.interval_seconds is None:
        args.interval_seconds = plan_config.interval_minutes * 60
    if args.interval_seconds < 60:
        LOGGER.error("interval-seconds must be at least 60")
        return 2

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Ingress requires the container interface; serve() enforces an IP filter.
    host = args.web_host or os.environ.get("VOLTWISE_WEB_HOST", "0.0.0.0")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    container_mode = host == "0.0.0.0"
    server = serve(output, port=args.web_port, host=host, container_mode=container_mode)
    threading.Thread(target=server.serve_forever, daemon=True, name="voltwise-webui").start()
    LOGGER.info("web UI listening on %s:%s (ingress mode=%s)", host, args.web_port, container_mode)

    cli_args = ["--config", args.config, "--output", str(output)]
    if args.apply:
        cli_args.append("--apply")
        if args.confirm:
            cli_args.append("--confirm")
    else:
        cli_args.append("--dry-run")

    # One client for the whole service lifetime keeps failure/cooldown state.
    needs_client = (
        plan_config.tariff.provider == "home_assistant"
        or plan_config.forecast.provider == "home_assistant"
        or plan_config.battery.soc_entity is not None
        or (plan_config.actuation.enabled and args.apply)
    )
    shared_client = None
    if needs_client:
        token = None
        if plan_config.actuation.ha_token_env:
            token = os.environ.get(plan_config.actuation.ha_token_env)
        shared_client = HomeAssistantClient(
            plan_config.actuation.ha_url,
            token=token,
            timeout_seconds=plan_config.tariff.timeout_seconds,
            dry_run=not args.apply,
            failure_cooldown_seconds=plan_config.actuation.failure_cooldown_seconds,
        )

    metrics = Metrics()
    metrics_path = output.parent / "metrics.json"
    try:
        while not _STOP.is_set():
            metrics.increment("cycles_total")
            try:
                parsed = build_parser().parse_args(cli_args)
                parsed.ha_client = shared_client
                code = run_once(parsed)
                metrics.set("last_cycle_epoch", time.time())
                if code == 0:
                    metrics.increment("cycles_ok")
                else:
                    metrics.increment("cycles_failed")
                    LOGGER.error("optimisation cycle exited with code %s", code)
            except (ConfigError, ValueError, OverflowError, RecursionError, OSError,
                        HomeAssistantError) as exc:
                metrics.increment("cycles_failed")
                LOGGER.error("cycle error: %s: %s", type(exc).__name__, exc)
            metrics.write(metrics_path)
            _STOP.wait(args.interval_seconds)
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

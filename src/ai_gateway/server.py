"""Entrypoint do gateway: bootstrap do ambiente e inicialização do LiteLLM Proxy."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
from pathlib import Path

from ai_gateway.bootstrap import ConfigError, prepare

log = logging.getLogger("ai_gateway.server")

DEFAULT_CONFIG = "/app/config/config.yaml"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ai-gateway", description=__doc__)
    parser.add_argument("--config", default=os.getenv("GATEWAY_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))  # noqa: S104 - roda em container
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "4000")))
    parser.add_argument(
        "--num-workers",
        type=int,
        default=int(os.getenv("GATEWAY_WORKERS", "1")),
        help="No Kubernetes prefira 1 worker por pod e escale por réplicas (HPA).",
    )
    parser.add_argument("--check", action="store_true", help="Só valida config/ambiente e sai.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=os.getenv("GATEWAY_LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s"
    )
    args = parse_args(argv)

    # Precisa existir antes do primeiro import de prometheus_client para que as métricas
    # de todos os processos (inclusive as customizadas) sejam agregadas no /metrics.
    if os.getenv("PROMETHEUS_METRICS_PORT"):
        os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", os.path.join(tempfile.gettempdir(), "prometheus"))

    rendered_path = Path(
        os.getenv("GATEWAY_RENDERED_CONFIG", os.path.join(tempfile.gettempdir(), "litellm.yaml"))
    )
    try:
        prepare(Path(args.config), rendered_path)
    except ConfigError as exc:
        log.error("Configuração inválida: %s", exc)
        sys.exit(2)

    if args.check:
        log.info("Configuração válida (%s)", args.config)
        return

    from litellm.proxy.proxy_cli import run_server

    run_server.main(
        args=[
            "--config",
            str(rendered_path),
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--num_workers",
            str(args.num_workers),
        ],
        prog_name="litellm",
    )


if __name__ == "__main__":
    main()

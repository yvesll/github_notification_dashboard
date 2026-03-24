#
# SPDX-FileCopyrightText: 2026 yvesll
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import socket

from gh_dashboard import create_app

app = create_app()


def choose_runtime_port(host: str, configured_port: int) -> tuple[int, int | None]:
    candidates = [configured_port]
    if configured_port == 5000:
        candidates.append(5055)
    candidates.extend(port for port in range(configured_port + 1, configured_port + 11) if port not in candidates)

    for port in candidates:
        if is_port_available(host, port):
            fallback_from = configured_port if port != configured_port else None
            return port, fallback_from

    raise RuntimeError(f"No available port found near {configured_port}")


def is_port_available(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        if sock.connect_ex((probe_host, port)) == 0:
            return False

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


if __name__ == "__main__":
    host = app.config["DASHBOARD_CONFIG"].host
    configured_port = app.config["DASHBOARD_CONFIG"].port
    runtime_port, fallback_from = choose_runtime_port(host, configured_port)

    if fallback_from is not None:
        print(
            f"Port {fallback_from} is already in use. "
            f"Starting GitHub Review Desk on http://{host}:{runtime_port} instead."
        )
    else:
        print(f"Starting GitHub Review Desk on http://{host}:{runtime_port}")

    app.run(
        host=host,
        port=runtime_port,
        debug=app.config["DASHBOARD_CONFIG"].debug,
    )

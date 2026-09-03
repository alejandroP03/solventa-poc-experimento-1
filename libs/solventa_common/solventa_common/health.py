"""Blueprint de health checks (kickoff §8.2).

Dos endpoints con propósitos distintos, y la distinción importa:

- `/health/live` — el proceso está vivo. **No toca ninguna dependencia.** Es el que
  usan ALB y ECS. Si consultara Redis o RabbitMQ, una caída del broker haría que
  ECS matara y reiniciara servicios sanos en cascada, convirtiendo una degradación
  parcial en una caída total. Justo lo contrario de lo que el ASR persigue.
- `/health/ready` — las dependencias críticas responden. Informativo para el
  operador y para el smoke test; nunca para el orquestador.

Un servicio debe arrancar y responder `/health/live` aunque Redis, Postgres o
RabbitMQ no estén listos todavía (§8.2: ECS no garantiza orden de arranque).
"""

from __future__ import annotations

from typing import Callable, Mapping

from flask import Blueprint, jsonify

# Un check devuelve (ok, detalle). Nunca lanza: una excepción no capturada aquí
# convertiría el endpoint de diagnóstico en un 500 sin información.
Check = Callable[[], tuple[bool, str]]


def build_blueprint(service_name: str, checks: Mapping[str, Check] | None = None) -> Blueprint:
    bp = Blueprint("health", __name__)
    checks = dict(checks or {})

    @bp.get("/health/live")
    def live():
        return jsonify(status="alive", service=service_name), 200

    @bp.get("/health/ready")
    def ready():
        results: dict[str, dict[str, object]] = {}
        all_ok = True
        for name, check in checks.items():
            try:
                ok, detail = check()
            except Exception as exc:  # noqa: BLE001 - el diagnóstico no debe romperse
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            results[name] = {"ok": ok, "detail": detail}
            all_ok = all_ok and ok

        status = "ready" if all_ok else "degraded"
        return jsonify(status=status, service=service_name, dependencies=results), (
            200 if all_ok else 503
        )

    # Alias de conveniencia para curl manual y para el mock, cuyo /health es
    # además el objetivo del Ping-Echo del Monitor.
    @bp.get("/health")
    def health():
        return jsonify(status="alive", service=service_name), 200

    return bp

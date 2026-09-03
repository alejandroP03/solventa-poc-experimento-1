"""El correlation_id debe existir en cada línea de log y en cada salto.

§3.5 le da dos funciones y ninguna más: casar réplicas en el request-reply y
trazabilidad diagnóstica. Estos tests fijan la propagación, no ningún uso como
clave de idempotencia — que el diseño excluye explícitamente.
"""

from __future__ import annotations

import json
import logging

import pytest
from flask import Flask

from solventa_common import correlation
from solventa_common.logging import JsonFormatter


@pytest.fixture
def app():
    application = Flask(__name__)

    @application.get("/echo")
    def echo():
        return {"correlation_id": correlation.get_correlation_id()}

    return application


def test_el_gateway_genera_el_id_si_no_viene(app):
    correlation.install(app, generate_if_missing=True)
    response = app.test_client().get("/echo")

    generado = response.json["correlation_id"]
    assert len(generado) == 36  # UUID4
    assert not generado.startswith("orphan-")
    assert response.headers[correlation.HEADER] == generado


def test_los_demas_servicios_marcan_la_peticion_sin_cabecera(app):
    # Una petición sin correlation_id a un servicio interno significa que alguien
    # saltó el gateway. El id sintético se marca para que se note en los logs en
    # lugar de disimularse como si el journey fuera normal.
    correlation.install(app, generate_if_missing=False)
    response = app.test_client().get("/echo")

    assert response.json["correlation_id"].startswith("orphan-")


def test_el_id_entrante_se_respeta_y_se_devuelve(app):
    correlation.install(app)
    entrante = "11111111-2222-3333-4444-555555555555"
    response = app.test_client().get("/echo", headers={correlation.HEADER: entrante})

    assert response.json["correlation_id"] == entrante
    assert response.headers[correlation.HEADER] == entrante


def test_las_cabeceras_salientes_llevan_el_contexto():
    correlation.set_correlation_id("abc-123")
    correlation.set_partner_id("socio-2")

    headers = correlation.outbound_headers({"Content-Type": "application/json"})

    assert headers[correlation.HEADER] == "abc-123"
    assert headers[correlation.PARTNER_HEADER] == "socio-2"
    assert headers["Content-Type"] == "application/json"


def test_bind_desde_amqp_reconstruye_el_contexto():
    class Props:
        correlation_id = "desde-la-cola"

    correlation.set_correlation_id("otro")
    correlation.bind_from_amqp(Props(), {"partner_id": "socio-3"})

    assert correlation.get_correlation_id() == "desde-la-cola"
    assert correlation.get_partner_id() == "socio-3"


def test_el_log_json_incluye_el_correlation_id_sin_pasarlo():
    # Reconstruir por qué una cotización salió DEGRADED durante la ventana de
    # fallo depende de que el id esté en *todas* las líneas, no solo en las que
    # alguien se acordó de anotarlo.
    correlation.set_correlation_id("trace-me")
    correlation.set_partner_id("socio-1")
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "perfil degradado", None, None
    )
    record.profile_quality = "DEGRADED"

    linea = json.loads(JsonFormatter("cotizacion").format(record))

    assert linea["correlation_id"] == "trace-me"
    assert linea["partner_id"] == "socio-1"
    assert linea["service"] == "cotizacion"
    assert linea["level"] == "INFO"
    assert linea["message"] == "perfil degradado"
    assert linea["profile_quality"] == "DEGRADED"  # los extras llegan al JSON
    assert "\n" not in linea["message"]

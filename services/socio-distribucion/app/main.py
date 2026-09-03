"""socio-distribucion (8081) — canal del socio embebido.

Expone la interfaz `Provider`: el precio del servicio propio del socio, que se
suma a la prima del seguro para componer el precio final que ve el cliente.

**Es la ruta no afectada que mide SP-5.** No depende de Open Finance ni de
RabbitMQ, así que su latencia debe permanecer estable mientras el perfilamiento se
satura. Si se degrada, la degradación viajó por un recurso compartido —hilos de
Gunicorn, pool de conexiones— y el bulkhead no está aislando.

Por eso este servicio se mantiene deliberadamente trivial: cualquier lentitud
observada en su p95 durante la ventana de fallo es atribuible al aislamiento, no
a su propia lógica.
"""

from __future__ import annotations

import hashlib
import time

from flask import Flask, jsonify, request

from solventa_common.app_factory import create_app
from solventa_common.config import load_config

# Latencia fija del servicio del socio. Hardcodeada (§0): no es variable de ningún
# SP, es la línea base contra la que se compara la desviación del p95 en SP-5.
PROVIDER_LATENCY_S = 0.015

# Precio base por producto, en la moneda ficticia del POC. Sin realismo de
# negocio (§2.3): solo hace falta que sea determinista y no cero.
PROVIDER_BASE_PRICE = {"VIAJE": 4200.0, "DISPOSITIVO": 2800.0, "VIDA_MICRO": 1500.0}
PROVIDER_FALLBACK_PRICE = 3000.0


def provider_price(product_code: str, partner_id: str) -> float:
    """Precio determinista del servicio del socio.

    Depende del socio para que los tres `X-Partner-Id` que rota k6 den precios
    distintos y un error de propagación de la cabecera sea visible en los datos.
    """
    base = PROVIDER_BASE_PRICE.get(product_code, PROVIDER_FALLBACK_PRICE)
    digest = hashlib.sha256(f"{partner_id}:{product_code}".encode()).digest()
    factor = 0.90 + (digest[0] / 255.0) * 0.20  # +-10 % estable por socio
    return round(base * factor, 2)


def create() -> Flask:
    cfg = load_config(service_name="socio-distribucion", default_port=8081)
    app = create_app(cfg)

    @app.get("/provider/price")
    def price():
        product_code = request.args.get("product_code", "")
        partner_id = request.headers.get("X-Partner-Id", "-")

        time.sleep(PROVIDER_LATENCY_S)

        return jsonify(
            product_code=product_code,
            partner_id=partner_id,
            provider_price=provider_price(product_code, partner_id),
            currency="COP",
        ), 200

    return app


app = create()

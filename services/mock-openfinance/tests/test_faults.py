"""El instrumento debe comportarse exactamente como la tabla de §4.

Estos tests protegen sobre todo la **asimetría deliberada de /health** en los modos
`slow` y `flaky`. Es el punto ciego que SP-4 debe cuantificar, y es justo el tipo
de cosa que alguien "arreglaría" por parecer un bug. Si estos tests se ponen en
rojo por hacer que /health falle en slow, el hallazgo de SP-4 desaparece.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.faults import MODE_CODES, TIMEOUT_SLEEP_S, FaultError, FaultInjector  # noqa: E402
from app.profiles import build_profile  # noqa: E402


@pytest.fixture
def injector():
    return FaultInjector()


def test_normal_responde_rapido_y_sano(injector):
    delay, status = injector.business_delay_and_status()
    assert status == 200
    assert 0.040 <= delay <= 0.090


def test_error_5xx_falla_negocio_y_health(injector):
    # Caída dura: ambas fuentes de detección de SP-4 la ven.
    injector.set_mode("error_5xx")
    assert injector.business_delay_and_status() == (0.0, 503)
    assert injector.health_status() == (0.0, 503)


def test_timeout_cuelga_negocio_y_health(injector):
    injector.set_mode("timeout")
    delay, _ = injector.business_delay_and_status()
    assert delay == TIMEOUT_SLEEP_S
    assert injector.health_status()[0] == TIMEOUT_SLEEP_S

    # El sleep debe superar cualquier timeout que el cliente pueda usar, incluido
    # el de 30 s del baseline: el cliente siempre se rinde primero.
    assert TIMEOUT_SLEEP_S > 30.0


def test_slow_arrastra_el_negocio_pero_health_miente(injector):
    # SP-4: el Ping-Echo declara sano al proveedor mientras el tráfico real se
    # arrastra. NO ARREGLAR: es el hallazgo, no un defecto.
    injector.set_mode("slow", latency_ms=1500)

    delay, status = injector.business_delay_and_status()
    assert status == 200
    assert 1.275 <= delay <= 1.725  # 1.5 s +- 15 %

    assert injector.health_status() == (0.0, 200), "/health debe mentir en modo slow"


def test_flaky_falla_intermitente_pero_health_miente(injector):
    # SP-4: solo el tráfico real revela el problema.
    injector.set_mode("flaky", failure_rate=0.5)

    fallos = sum(1 for _ in range(400) if injector.business_delay_and_status()[1] == 503)
    assert 160 <= fallos <= 240, f"tasa fuera de rango: {fallos}/400"

    assert injector.health_status() == (0.0, 200), "/health debe mentir en modo flaky"


def test_failure_rate_se_respeta_en_los_extremos(injector):
    injector.set_mode("flaky", failure_rate=0.0)
    assert all(injector.business_delay_and_status()[1] == 200 for _ in range(50))

    injector.set_mode("flaky", failure_rate=1.0)
    assert all(injector.business_delay_and_status()[1] == 503 for _ in range(50))


def test_la_secuencia_flaky_es_reproducible_entre_corridas():
    # Sin esto, el flapping medido en SP-2 variaría entre corridas por azar y no
    # por la configuración del breaker, que es la variable independiente.
    def secuencia():
        inj = FaultInjector()
        inj.set_mode("flaky", failure_rate=0.5)
        return [inj.business_delay_and_status()[1] for _ in range(40)]

    assert secuencia() == secuencia()


@pytest.mark.parametrize(
    "kwargs, fragmento",
    [
        ({"mode": "caido"}, "desconocido"),
        ({"mode": "flaky", "failure_rate": 1.5}, "failure_rate"),
        ({"mode": "slow", "latency_ms": -1}, "latency_ms"),
        ({"mode": "normal", "duration_s": 0}, "duration_s"),
    ],
)
def test_parametros_invalidos_se_rechazan(injector, kwargs, fragmento):
    with pytest.raises(FaultError, match=fragmento):
        injector.set_mode(kwargs.pop("mode"), **kwargs)


def test_todos_los_modos_tienen_codigo_para_el_gauge():
    # El overlay de la ventana de fallo en Grafana depende de este mapeo.
    from app.faults import MODES

    assert set(MODES) == set(MODE_CODES)


def test_el_perfil_es_determinista_por_client_id():
    # La prima depende del perfil; si el perfil variara entre llamadas, la
    # comparación de pricing FRESH/DEGRADED/DEFAULT de SP-3 mezclaría la
    # degradación con ruido del generador.
    a, b = build_profile("CLI-0042"), build_profile("CLI-0042")
    del a["generated_at"], b["generated_at"]
    assert a == b
    assert build_profile("CLI-0043") != a

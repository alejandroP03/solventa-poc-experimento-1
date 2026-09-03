"""El bulkhead debe rechazar, no encolar.

# TÁCTICA: bulkhead — SP-5
Un pool que encola no aísla: la espera sigue reteniendo el hilo de Gunicorn, que
es el recurso que SP-5 quiere proteger. Estos tests fijan esa semántica.
"""

from __future__ import annotations

import threading

import pytest

from solventa_common.http_client import BoundedPool, PoolRejected


def test_acepta_hasta_el_maximo_y_rechaza_el_siguiente():
    pool = BoundedPool("openfinance", max_size=2)
    held = []

    with pool.slot():
        held.append(1)
        with pool.slot():
            held.append(2)
            assert pool.inflight == 2
            with pytest.raises(PoolRejected) as exc:
                with pool.slot():
                    held.append(3)

    assert held == [1, 2]
    assert exc.value.pool == "openfinance"
    assert pool.inflight == 0


def test_el_slot_se_libera_aunque_el_cuerpo_lance():
    # Si una excepción filtrara un slot, el pool se agotaría solo con el paso del
    # tiempo y SP-5 mediría una fuga en lugar de la táctica.
    pool = BoundedPool("provider", max_size=1)

    with pytest.raises(ValueError):
        with pool.slot():
            raise ValueError("fallo de la dependencia")

    assert pool.inflight == 0
    with pool.slot():
        assert pool.inflight == 1


def test_desactivado_deja_pasar_todo():
    # Ablación de SP-5: sin bulkhead no hay rechazo, la saturación se propaga.
    # Es la condición contra la que se compara y debe degradarse de forma observable.
    pool = BoundedPool("pending_replies", max_size=1, enabled=False)

    with pool.slot():
        with pool.slot():
            with pool.slot():
                assert pool.inflight == 3


def test_rechazo_es_inmediato_por_defecto():
    # acquire_timeout_s=0: el rechazo no debe costar latencia. Un bulkhead que
    # hace esperar antes de rechazar suma lo peor de las dos opciones.
    pool = BoundedPool("openfinance", max_size=1)
    liberado = threading.Event()

    with pool.slot():
        started = threading.Event()

        def intentar():
            started.set()
            with pytest.raises(PoolRejected):
                with pool.slot():
                    pass
            liberado.set()

        hilo = threading.Thread(target=intentar)
        hilo.start()
        started.wait(timeout=1)
        assert liberado.wait(timeout=0.5), "el rechazo debió ser inmediato, no una espera"
        hilo.join()


def test_espera_acotada_cede_el_slot_liberado():
    # Con acquire_timeout_s > 0 el pool tolera picos cortos: el segundo hilo entra
    # en cuanto el primero libera, en lugar de ser rechazado.
    pool = BoundedPool("provider", max_size=1, acquire_timeout_s=2.0)
    entro = threading.Event()

    def esperar():
        with pool.slot():
            entro.set()

    with pool.slot():
        hilo = threading.Thread(target=esperar)
        hilo.start()
        assert not entro.wait(timeout=0.2), "no debía entrar mientras el slot está tomado"

    hilo.join(timeout=3)
    assert entro.is_set()

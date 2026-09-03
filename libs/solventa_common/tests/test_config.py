"""La configuración debe fallar ruidosamente, no degradar en silencio.

Un default silencioso ante una variable mal escrita invalidaría una corrida de
240 s sin avisar, y el resultado se leería como un hallazgo del experimento.
"""

from __future__ import annotations

import pytest

from solventa_common.config import BASELINE_TIMEOUT_S, ConfigError, load_config

BASE_ENV = {"SERVICE_NAME": "test-service", "PORT": "8099"}


def _load(monkeypatch, **overrides):
    for key in list(overrides) + list(BASE_ENV):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**BASE_ENV, **overrides}.items():
        monkeypatch.setenv(key, str(value))
    return load_config()


def test_defaults_coinciden_con_el_env_example(monkeypatch):
    cfg = _load(monkeypatch)
    assert cfg.openfinance_timeout_ms == 700
    assert cfg.breaker_fail_max == 5
    assert cfg.breaker_reset_timeout_s == 30
    assert cfg.profile_cache_ttl_s == 300
    assert cfg.profile_cache_stale_grace_s == 1800
    assert cfg.cache_preload_ratio == 0.5
    assert cfg.monitor_interval_ms == 2000
    assert cfg.pool_provider_max == 8
    assert cfg.journey_latency_budget_ms == 250
    assert cfg.quote_mode == "treatment"
    assert cfg.consumer_mode == "single_active"


def test_variable_obligatoria_ausente_falla(monkeypatch):
    monkeypatch.delenv("SERVICE_NAME", raising=False)
    monkeypatch.setenv("PORT", "8099")
    with pytest.raises(ConfigError, match="SERVICE_NAME"):
        load_config()


@pytest.mark.parametrize(
    "overrides, fragmento",
    [
        ({"OPENFINANCE_TIMEOUT_MS": "setecientos"}, "no es un entero"),
        ({"BREAKER_ENABLED": "quizás"}, "no es booleano"),
        ({"QUOTE_MODE": "produccion"}, "no está entre"),
        ({"CONSUMER_MODE": "round_robin"}, "no está entre"),
        ({"CACHE_PRELOAD_RATIO": "1.5"}, "debe ser <= 1.0"),
        ({"CACHE_PRELOAD_RATIO": "-0.1"}, "debe ser >= 0.0"),
        ({"BREAKER_FAIL_MAX": "0"}, "debe ser >= 1"),
    ],
)
def test_valores_invalidos_fallan_al_arrancar(monkeypatch, overrides, fragmento):
    with pytest.raises(ConfigError, match=fragmento):
        _load(monkeypatch, **overrides)


def test_reply_timeout_debe_superar_al_timeout_de_openfinance(monkeypatch):
    # Si la espera de la réplica vence antes que la llamada saliente, toda
    # cotización saldría DEFAULT por vencimiento del presupuesto y SP-1 dejaría
    # de ser medible: la variable independiente no tendría efecto observable.
    with pytest.raises(ConfigError, match="REPLY_TIMEOUT_MS"):
        _load(monkeypatch, REPLY_TIMEOUT_MS="500", OPENFINANCE_TIMEOUT_MS="700")


def test_baseline_ignora_los_flags_de_ablacion(monkeypatch):
    # §3.4: los flags de ablación solo aplican en treatment. El baseline es por
    # definición la ausencia de todas las tácticas.
    cfg = _load(
        monkeypatch,
        QUOTE_MODE="baseline",
        BREAKER_ENABLED="true",
        CACHE_ENABLED="true",
        BULKHEAD_ENABLED="true",
        MONITOR_SIGNAL_ENABLED="true",
    )
    assert cfg.tactic_enabled("breaker") is False
    assert cfg.tactic_enabled("cache") is False
    assert cfg.tactic_enabled("bulkhead") is False
    assert cfg.tactic_enabled("monitor_signal") is False
    assert cfg.openfinance_timeout_s == BASELINE_TIMEOUT_S


def test_treatment_respeta_los_flags_de_ablacion(monkeypatch):
    cfg = _load(monkeypatch, QUOTE_MODE="treatment", BREAKER_ENABLED="false")
    assert cfg.tactic_enabled("breaker") is False
    assert cfg.tactic_enabled("cache") is True
    assert cfg.openfinance_timeout_s == 0.7

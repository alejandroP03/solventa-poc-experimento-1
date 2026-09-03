"""Carga y validación de la configuración del experimento.

Todo parámetro viene de una variable de entorno (kickoff §5.3: ningún hostname,
puerto ni parámetro de táctica puede estar hardcodeado) y cada uno se agrupa bajo
el punto de sensibilidad que lo justifica.

Regla de diseño: ante un valor inválido este módulo **falla al arrancar**. Un
default silencioso ante una variable mal escrita invalidaría una corrida entera de
240 s sin que nadie se entere, y el resultado se leería como un hallazgo cuando en
realidad es un error de configuración. En un instrumento de medición, fallar
ruidosamente es más barato que medir mal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Sequence


class ConfigError(RuntimeError):
    """Configuración ausente, mal formada o fuera de rango."""


# --------------------------------------------------------------------------- #
# Lectores primitivos                                                          #
# --------------------------------------------------------------------------- #

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _raw(name: str, default: str | None) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        if default is None:
            raise ConfigError(f"Falta la variable de entorno obligatoria {name!r}")
        return default
    return value.strip()


def get_str(name: str, default: str | None = None, *, choices: Sequence[str] | None = None) -> str:
    value = _raw(name, default)
    if choices is not None and value not in choices:
        raise ConfigError(f"{name}={value!r} no está entre {list(choices)}")
    return value


def get_int(name: str, default: int | None = None, *, min_value: int | None = None) -> int:
    raw = _raw(name, None if default is None else str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} no es un entero") from exc
    if min_value is not None and value < min_value:
        raise ConfigError(f"{name}={value} debe ser >= {min_value}")
    return value


def get_float(
    name: str,
    default: float | None = None,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    raw = _raw(name, None if default is None else str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} no es un número") from exc
    if min_value is not None and value < min_value:
        raise ConfigError(f"{name}={value} debe ser >= {min_value}")
    if max_value is not None and value > max_value:
        raise ConfigError(f"{name}={value} debe ser <= {max_value}")
    return value


def get_bool(name: str, default: bool | None = None) -> bool:
    raw = _raw(name, None if default is None else str(default).lower()).lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name}={raw!r} no es booleano (usar true/false)")


# --------------------------------------------------------------------------- #
# Configuración                                                                #
# --------------------------------------------------------------------------- #

QUOTE_MODES = ("treatment", "baseline")
CONSUMER_MODES = ("single_active", "competing")
ROLES = ("PRIMARY", "BACKUP", "")

# Timeout del modo baseline. Hardcodeado a propósito: no es la variable
# independiente de ningún SP, es la definición misma del control de SP-0 (§3.4,
# "cadena bloqueante con timeout de 30 s"). Parametrizarlo sería ruido.
BASELINE_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class Config:
    """Instantánea inmutable del entorno, leída una vez al arrancar."""

    # --- Identidad del servicio ---
    service_name: str
    port: int
    role: str

    # --- Modos de ejecución (§3.4) ---
    quote_mode: str
    consumer_mode: str

    # --- SP-1: timeout hacia Open Finance ---
    openfinance_timeout_ms: int

    # --- SP-2: circuit breaker ---
    breaker_fail_max: int
    breaker_reset_timeout_s: int
    breaker_enabled: bool

    # --- SP-3: caché de perfil ---
    profile_cache_ttl_s: int
    profile_cache_stale_grace_s: int
    cache_enabled: bool
    cache_preload_ratio: float

    # --- SP-4: detección ---
    monitor_signal_enabled: bool
    monitor_interval_ms: int
    monitor_unhealthy_threshold: int
    monitor_timeout_ms: int
    monitor_healthy_threshold: int

    # --- SP-5: aislamiento de recursos ---
    bulkhead_enabled: bool
    pool_openfinance_max: int
    pool_pending_replies_max: int
    pool_provider_max: int

    # --- Presupuesto del journey ---
    journey_latency_budget_ms: int
    reply_timeout_ms: int

    # --- Infraestructura ---
    redis_url: str
    database_url: str
    rabbitmq_url: str
    rabbitmq_mgmt_url: str
    exchange_quotes: str
    queue_requests: str
    exchange_events: str

    # --- Endpoints entre servicios ---
    cotizacion_url: str
    socio_url: str
    profiler_url: str
    openfinance_url: str

    # --- Logging ---
    log_level: str
    log_format: str

    @property
    def openfinance_timeout_s(self) -> float:
        """Timeout efectivo hacia Open Finance, en segundos.

        En `baseline` no hay táctica de timeout acotado: la cadena es bloqueante
        con 30 s (§3.4). Es lo que hace que el modo control sature los workers y
        propague 5xx, que es exactamente lo que SP-0 debe demostrar.
        """
        if self.quote_mode == "baseline":
            return BASELINE_TIMEOUT_S
        return self.openfinance_timeout_ms / 1000.0

    @property
    def reply_timeout_s(self) -> float:
        return self.reply_timeout_ms / 1000.0

    @property
    def is_baseline(self) -> bool:
        return self.quote_mode == "baseline"

    def tactic_enabled(self, tactic: str) -> bool:
        """¿Está activa una táctica?

        Los flags de ablación solo aplican en `treatment` (§3.4): el modo
        `baseline` es por definición la ausencia de todas las tácticas, así que
        un `BREAKER_ENABLED=true` heredado del entorno no debe reactivarlas.
        """
        if self.is_baseline:
            return False
        return {
            "breaker": self.breaker_enabled,
            "cache": self.cache_enabled,
            "monitor_signal": self.monitor_signal_enabled,
            "bulkhead": self.bulkhead_enabled,
        }[tactic]

    def as_dict(self) -> dict[str, object]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


def load_config(service_name: str | None = None, default_port: int | None = None) -> Config:
    """Lee el entorno completo. Lanza ConfigError ante cualquier valor inválido."""

    cfg = Config(
        service_name=service_name or get_str("SERVICE_NAME"),
        port=default_port if default_port is not None else get_int("PORT", min_value=1),
        role=get_str("ROLE", "", choices=ROLES),
        quote_mode=get_str("QUOTE_MODE", "treatment", choices=QUOTE_MODES),
        consumer_mode=get_str("CONSUMER_MODE", "single_active", choices=CONSUMER_MODES),
        openfinance_timeout_ms=get_int("OPENFINANCE_TIMEOUT_MS", 700, min_value=1),
        breaker_fail_max=get_int("BREAKER_FAIL_MAX", 5, min_value=1),
        breaker_reset_timeout_s=get_int("BREAKER_RESET_TIMEOUT_S", 30, min_value=1),
        breaker_enabled=get_bool("BREAKER_ENABLED", True),
        profile_cache_ttl_s=get_int("PROFILE_CACHE_TTL_S", 300, min_value=1),
        profile_cache_stale_grace_s=get_int("PROFILE_CACHE_STALE_GRACE_S", 1800, min_value=0),
        cache_enabled=get_bool("CACHE_ENABLED", True),
        cache_preload_ratio=get_float("CACHE_PRELOAD_RATIO", 0.5, min_value=0.0, max_value=1.0),
        monitor_signal_enabled=get_bool("MONITOR_SIGNAL_ENABLED", True),
        monitor_interval_ms=get_int("MONITOR_INTERVAL_MS", 2000, min_value=100),
        monitor_unhealthy_threshold=get_int("MONITOR_UNHEALTHY_THRESHOLD", 2, min_value=1),
        monitor_timeout_ms=get_int("MONITOR_TIMEOUT_MS", 500, min_value=1),
        monitor_healthy_threshold=get_int("MONITOR_HEALTHY_THRESHOLD", 2, min_value=1),
        bulkhead_enabled=get_bool("BULKHEAD_ENABLED", True),
        pool_openfinance_max=get_int("POOL_OPENFINANCE_MAX", 8, min_value=1),
        pool_pending_replies_max=get_int("POOL_PENDING_REPLIES_MAX", 16, min_value=1),
        pool_provider_max=get_int("POOL_PROVIDER_MAX", 8, min_value=1),
        journey_latency_budget_ms=get_int("JOURNEY_LATENCY_BUDGET_MS", 250, min_value=1),
        reply_timeout_ms=get_int("REPLY_TIMEOUT_MS", 900, min_value=1),
        redis_url=get_str("REDIS_URL", "redis://redis:6379/0"),
        database_url=get_str(
            "DATABASE_URL", "postgresql+psycopg://solventa:solventa@postgres:5432/solventa"
        ),
        rabbitmq_url=get_str("RABBITMQ_URL", "amqp://solventa:solventa@rabbitmq:5672/"),
        rabbitmq_mgmt_url=get_str("RABBITMQ_MGMT_URL", "http://rabbitmq:15672"),
        exchange_quotes=get_str("EXCHANGE_QUOTES", "solventa.quotes"),
        queue_requests=get_str("QUEUE_REQUESTS", "cotizacion.requests"),
        exchange_events=get_str("EXCHANGE_EVENTS", "solventa.events"),
        cotizacion_url=get_str("COTIZACION_URL", "http://cotizacion:8082"),
        socio_url=get_str("SOCIO_URL", "http://socio-distribucion:8081"),
        profiler_url=get_str("PROFILER_URL", "http://financial-profiler:8085"),
        openfinance_url=get_str("OPENFINANCE_URL", "http://mock-openfinance:8090"),
        log_level=get_str("LOG_LEVEL", "INFO").upper(),
        log_format=get_str("LOG_FORMAT", "json", choices=("json", "text")),
    )
    _validate_coherence(cfg)
    return cfg


def _validate_coherence(cfg: Config) -> None:
    """Comprobaciones que cruzan más de una variable.

    No corrigen valores: solo impiden arrancar con una combinación que produciría
    una medición sin sentido y que se leería como un hallazgo.
    """
    if cfg.reply_timeout_ms <= cfg.openfinance_timeout_ms:
        raise ConfigError(
            f"REPLY_TIMEOUT_MS={cfg.reply_timeout_ms} debe superar "
            f"OPENFINANCE_TIMEOUT_MS={cfg.openfinance_timeout_ms}: si la espera de la "
            "réplica vence antes que la llamada a Open Finance, toda cotización saldría "
            "DEFAULT por vencimiento del presupuesto y SP-1 dejaría de ser medible."
        )

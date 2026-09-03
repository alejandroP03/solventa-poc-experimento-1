"""Librería compartida del POC Solventa.

Contiene lo transversal a los nueve servicios: configuración, logging estructurado,
métricas, correlación, health checks, cliente HTTP con bulkhead y la abstracción
sobre el broker. La lógica de arquitectura vive en cada servicio.
"""

__all__ = [
    "app_factory",
    "config",
    "correlation",
    "health",
    "http_client",
    "logging",
    "messaging",
    "metrics",
]

__version__ = "0.1.0"

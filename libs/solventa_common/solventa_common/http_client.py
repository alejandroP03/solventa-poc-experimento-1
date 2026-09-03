"""Cliente HTTP con timeout obligatorio y pool acotado.

# TÁCTICA: bulkhead (aislamiento de recursos) — SP-5
# TÁCTICA: timeout acotado — SP-1

Sede de dos de las cinco tácticas bajo prueba. El pool acotado se implementa con
un semáforo explícito además del `pool_maxsize` del adaptador de urllib3, porque
urllib3 **encola** las peticiones que exceden el pool en lugar de rechazarlas: la
espera seguiría reteniendo el hilo de Gunicorn, que es justamente el recurso que
SP-5 quiere proteger. Un bulkhead que encola no aísla, solo desplaza la cola.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

import requests
from requests.adapters import HTTPAdapter

from . import metrics
from .correlation import outbound_headers


class PoolRejected(RuntimeError):
    """No había slot libre en el pool.

    Quien lo captura **nunca** lo traduce en 5xx: el invariante duro de §3.1 exige
    que todo fallo aguas abajo se convierta en una degradación del profile_quality.
    """

    def __init__(self, pool: str) -> None:
        super().__init__(f"pool {pool!r} lleno")
        self.pool = pool


class BoundedPool:
    """Semáforo acotado con métricas de ocupación, espera y rechazo.

    `acquire_timeout_s=0` es rechazo inmediato, que es lo que protege un hilo de
    Gunicorn. Un valor mayor tolera picos cortos a costa de retener el hilo
    durante la espera.
    """

    def __init__(
        self,
        name: str,
        max_size: int,
        *,
        enabled: bool = True,
        acquire_timeout_s: float = 0.0,
    ) -> None:
        self.name = name
        self.max_size = max_size
        self.enabled = enabled
        self.acquire_timeout_s = acquire_timeout_s
        self._semaphore = threading.BoundedSemaphore(max_size)
        self._inflight = 0
        self._lock = threading.Lock()
        metrics.pool_inflight.labels(pool=name).set(0)

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Reserva un slot o lanza PoolRejected.

        Con el bulkhead desactivado (ablación de SP-5) el pool deja pasar todo sin
        contabilizar rechazos: es la condición sin aislamiento contra la que se
        compara, y debe degradarse de forma observable.
        """
        if not self.enabled:
            self._enter()
            try:
                yield
            finally:
                self._exit()
            return

        started = time.perf_counter()
        acquired = self._semaphore.acquire(
            blocking=self.acquire_timeout_s > 0, timeout=self.acquire_timeout_s or None
        )
        metrics.pool_wait_seconds.labels(pool=self.name).observe(
            time.perf_counter() - started
        )
        if not acquired:
            metrics.pool_rejected_total.labels(pool=self.name).inc()
            raise PoolRejected(self.name)

        self._enter()
        try:
            yield
        finally:
            self._exit()
            self._semaphore.release()

    def _enter(self) -> None:
        with self._lock:
            self._inflight += 1
        metrics.pool_inflight.labels(pool=self.name).inc()

    def _exit(self) -> None:
        with self._lock:
            self._inflight -= 1
        metrics.pool_inflight.labels(pool=self.name).dec()


class HttpClient:
    """Session con timeout obligatorio, pool acotado y propagación de contexto."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float,
        pool: BoundedPool | None = None,
        max_connections: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.pool = pool

        # El pool de conexiones acompaña al semáforo: sostener más sockets de los
        # que el bulkhead permite en vuelo solo serviría para mantener abiertas
        # conexiones ociosas contra una dependencia que ya está colgada.
        size = max_connections or (pool.max_size if pool else 10)
        adapter = HTTPAdapter(pool_connections=size, pool_maxsize=size, max_retries=0)
        self._session = requests.Session()
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        timeout_s: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> requests.Response:
        """Ejecuta la petición. Puede lanzar PoolRejected o requests.Timeout.

        Ninguna de las dos se traduce nunca en 5xx hacia el socio; el llamante las
        convierte en una degradación del perfil.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        effective_timeout = self.timeout_s if timeout_s is None else timeout_s

        if self.pool is None:
            return self._send(method, url, json, effective_timeout, headers)
        with self.pool.slot():
            return self._send(method, url, json, effective_timeout, headers)

    def _send(
        self,
        method: str,
        url: str,
        json: Any,
        timeout_s: float,
        headers: Mapping[str, str] | None,
    ) -> requests.Response:
        return self._session.request(
            method,
            url,
            json=json,
            headers=outbound_headers(headers),
            # Tupla (connect, read): sin el timeout de conexión, un proveedor que
            # deja de aceptar sockets colgaría indefinidamente pese al read timeout.
            timeout=(min(timeout_s, 2.0), timeout_s),
        )

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def close(self) -> None:
        self._session.close()

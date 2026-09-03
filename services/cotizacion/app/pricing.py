"""Cálculo de la prima — determinista y sin realismo actuarial (§10).

    prima = tarifa_base[producto] x f_edad(age) x f_riesgo(perfil) x f_monto(monto)

Lo único que importa aquí para el experimento es que **la calidad del perfil
cambie el precio de forma observable**: si `DEFAULT` produjera la misma prima que
`FRESH`, la "cobertura de degradación" de SP-3 no tendría consecuencia medible y
el hallazgo de la caché fría —disponibilidad intacta, precisión colapsada— se
quedaría sin evidencia numérica.

El catálogo vive aquí como constante y se moverá a PostgreSQL en la fase de Tier 2
(§2.2) sin cambiar la firma de `compute_premium`.
"""

from __future__ import annotations

from typing import Any

TARIFA_BASE = {"VIAJE": 120000.0, "DISPOSITIVO": 85000.0, "VIDA_MICRO": 45000.0}
TARIFA_FALLBACK = 100000.0

# Factores conservadores fijos que se aplican con perfil DEFAULT. Conservador =
# más caro: sin información del cliente, el precio preliminar asume el peor caso.
# Es lo que hace visible en el pricing el costo de una caché fría.
DEFAULT_RISK_FACTOR = 1.35

INCOME_BAND_FACTOR = {
    "LOW": 1.25,
    "MEDIUM_LOW": 1.15,
    "MEDIUM": 1.00,
    "MEDIUM_HIGH": 0.92,
    "HIGH": 0.85,
}


def f_edad(age: int) -> float:
    if age < 25:
        return 1.20
    if age < 40:
        return 1.00
    if age < 60:
        return 1.15
    return 1.45


def f_monto(insured_amount: float) -> float:
    """Descuento por volumen, acotado para que el monto no domine la prima."""
    if insured_amount <= 1_000_000:
        return 1.00
    if insured_amount <= 5_000_000:
        return 0.95
    if insured_amount <= 20_000_000:
        return 0.90
    return 0.85


def f_riesgo(profile: dict[str, Any] | None) -> float:
    """Factor de riesgo derivado del perfil financiero.

    Sin perfil se aplica el factor conservador fijo. La diferencia entre ese
    valor y el que habría salido del perfil real es, literalmente, el error de
    pricing que la degradación introduce.
    """
    if not profile:
        return DEFAULT_RISK_FACTOR

    income = INCOME_BAND_FACTOR.get(profile.get("income_band", "MEDIUM"), 1.00)
    debt_ratio = float(profile.get("debt_ratio", 0.5))
    score = float(profile.get("payment_behavior_score", 500.0))
    stability = float(profile.get("stability_index", 0.5))

    # Score alto y estabilidad alta abaratan; endeudamiento alto encarece.
    behaviour = 1.30 - (score - 300.0) / 550.0 * 0.45
    factor = income * behaviour * (1.0 + debt_ratio * 0.25) * (1.15 - stability * 0.25)
    return round(max(0.60, min(factor, 2.00)), 4)


def compute_premium(
    *,
    product_code: str,
    age: int,
    insured_amount: float,
    profile: dict[str, Any] | None,
) -> dict[str, Any]:
    """Devuelve la prima y los factores aplicados.

    Los factores viajan en la respuesta para que el informe pueda mostrar de dónde
    sale la diferencia de precio entre una cotización FRESH y una DEFAULT sin
    tener que reconstruir el cálculo.
    """
    base = TARIFA_BASE.get(product_code, TARIFA_FALLBACK)
    factor_edad = f_edad(age)
    factor_riesgo = f_riesgo(profile)
    factor_monto = f_monto(insured_amount)
    premium = base * factor_edad * factor_riesgo * factor_monto

    return {
        "premium": round(premium, 2),
        "factors": {
            "tarifa_base": base,
            "f_edad": factor_edad,
            "f_riesgo": factor_riesgo,
            "f_monto": factor_monto,
        },
    }

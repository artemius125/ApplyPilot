from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Эндпоинт aitunnel для чтения баланса (только GET, ничего не списывает):
#   GET https://api.aitunnel.ru/v1/aitunnel/balance
#   Authorization: Bearer sk-aitunnel-...
# Ответ: {"balance": 296.47} — баланс аккаунта в рублях.
# Поле "budget" (остаток бюджета ключа, ₽) присутствует, только если бюджет задан.
BALANCE_PATH = "/v1/aitunnel/balance"


def _extract_balance(payload: Any) -> float | None:
    """Достать баланс в рублях из ответа API. Возвращает None при неизвестной форме."""
    if isinstance(payload, dict):
        value = payload.get("balance")
        if isinstance(value, bool):  # bool — подкласс int, отсекаем отдельно
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip().replace(",", "."))
            except ValueError:
                return None
    return None


def fetch_balance(
    api_key: str,
    base_url: str = "https://api.aitunnel.ru",
    *,
    timeout: float = 8.0,
    get: Callable[..., Any] | None = None,
) -> float | None:
    """Вернуть баланс аккаунта aitunnel в рублях или None при любой неудаче.

    Никогда не бросает исключений: сетевые ошибки, отказ авторизации и
    неизвестная форма ответа приводят к None. Ничего не пишет в stdout.

    ``get`` — инъекция httpx-подобного вызывающего объекта для тестов
    (сигнатура ``get(url, headers=..., timeout=...)`` -> объект с ``.json()``
    и, по возможности, ``.raise_for_status()``). По умолчанию используется httpx.
    """
    key = (api_key or "").strip()
    if not key:
        return None
    url = base_url.rstrip("/") + BALANCE_PATH
    headers = {"Authorization": f"Bearer {key}"}

    try:
        if get is None:
            import httpx

            with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
                response = client.get(url, headers=headers)
        else:
            response = get(url, headers=headers, timeout=timeout)

        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()
        payload = response.json()
    except Exception:  # noqa: BLE001 — по контракту никогда не бросаем
        return None

    return _extract_balance(payload)

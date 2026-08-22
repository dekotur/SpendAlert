import json
import os
import tempfile
import logging
import httpx
from utils import now_msk

import config

logger = logging.getLogger(__name__)

CACHE_FILE = config.CACHE_FILE

# In-process cache of the last-known-good rates. Populated on first read (or
# on every successful update_exchange_rate) so that routine /list-style reads
# never touch the filesystem — only the periodic 6h refresh job does.
_rates_mem: dict[str, float] | None = None

SETTINGS_CURRENCIES = ("RUB", "USD", "EUR", "GBP", "CNY")
SUPPORTED_CURRENCIES = SETTINGS_CURRENCIES

CURRENCY_SYMBOLS = {
    "RUB": "₽",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "CNY": "¥",
}

CURRENCY_NAMES = {
    "RUB": "Ruble",
    "USD": "Dollar",
    "EUR": "Euro",
    "GBP": "Pound",
    "CNY": "Yuan",
}


def currency_symbol(code: str) -> str:
    return CURRENCY_SYMBOLS.get((code or "RUB").upper(), code or "?")


def currency_label(code: str) -> str:
    code = (code or "RUB").upper()
    name = CURRENCY_NAMES.get(code, code)
    return f"{currency_symbol(code)} {name} ({code})"


def get_exchange_rates() -> dict[str, float] | None:
    """Return cached USD-based rates {CUR: units per 1 USD}. Served from an
    in-process cache after the first call — avoids a disk read (and, unlike
    a raw file read, can never observe a write-in-progress) on every /list
    or currency conversion."""
    global _rates_mem
    if _rates_mem is not None:
        return _rates_mem

    cache = _load_cache()
    if not cache:
        return None
    if "rates" in cache:
        _rates_mem = cache["rates"]
        return _rates_mem
    rub = cache.get("rate")
    if rub:
        _rates_mem = {"RUB": float(rub)}
        return _rates_mem
    return None


def get_usd_rub_rate():
    """Get USD/RUB exchange rate. Returns cached rate only (non-blocking)."""
    rates = get_exchange_rates()
    if rates and "RUB" in rates:
        return rates["RUB"]
    logger.warning("No cached exchange rate available")
    return None


def convert_amount(amount: float, from_currency: str, to_currency: str, rates: dict[str, float]) -> float:
    """Convert amount between currencies using USD as cross-rate."""
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency == to_currency:
        return amount

    def _to_usd(value: float, currency: str) -> float:
        if currency == "USD":
            return value
        rate = rates.get(currency)
        if not rate:
            raise ValueError(f"No exchange rate for {currency}")
        return value / rate

    def _from_usd(value_usd: float, currency: str) -> float:
        if currency == "USD":
            return value_usd
        rate = rates.get(currency)
        if not rate:
            raise ValueError(f"No exchange rate for {currency}")
        return value_usd * rate

    usd_amount = _to_usd(amount, from_currency)
    return _from_usd(usd_amount, to_currency)


async def update_exchange_rate():
    """Update exchange rates in background."""
    try:
        rates = await _fetch_from_primary()
        if not rates:
            rates = await _fetch_from_fallback()

        if rates:
            _save_cache(rates)
            rub = rates.get("RUB")
            logger.info("Exchange rates updated (USD/RUB=%s, %d currencies)", rub, len(rates))
            return rates
        logger.error("Failed to update exchange rate from all sources")
        return None
    except Exception as e:
        logger.error("Error updating exchange rate: %s", e)
        return None


def _load_cache():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error("Error loading cache: %s", e)
    return None


def _save_cache(rates: dict[str, float]):
    global _rates_mem
    try:
        cache = {
            "rates": rates,
            "rate": rates.get("RUB"),
            "timestamp": now_msk().isoformat(),
        }
        cache_dir = os.path.dirname(CACHE_FILE) or "."
        fd, tmp_path = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp_path, CACHE_FILE)  # atomic — readers never see a partial write
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        _rates_mem = rates
        logger.info("Cached %d exchange rates", len(rates))
    except Exception as e:
        logger.error("Error saving cache: %s", e)


def _normalize_rates(raw: dict) -> dict[str, float]:
    rates = {}
    for code, value in raw.items():
        if value is None:
            continue
        try:
            rates[code.upper()] = float(value)
        except (TypeError, ValueError):
            continue
    return rates


async def _fetch_from_primary():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://open.er-api.com/v6/latest/USD", timeout=5)
            if response.status_code == 200:
                data = response.json()
                rates = _normalize_rates(data.get("rates", {}))
                if rates.get("RUB"):
                    logger.info("Got rates from primary API (%d currencies)", len(rates))
                    return rates
    except Exception as e:
        logger.warning("Primary API failed: %s", e)
    return None


async def _fetch_from_fallback():
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
                timeout=5,
            )
            if response.status_code == 200:
                data = response.json()
                rates = {k.upper(): float(v) for k, v in data.get("usd", {}).items() if v is not None}
                if rates.get("RUB"):
                    logger.info("Got rates from fallback (%d currencies)", len(rates))
                    return rates
    except Exception as e:
        logger.warning("Fallback failed: %s", e)
    return None

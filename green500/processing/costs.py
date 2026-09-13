"""Currency-preserving estimates from dated provider rates and reported token counts."""

from decimal import Decimal


def _tokens(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Token counts must be explicit nonnegative integers.")
    return Decimal(value)


def api_cost_cny(
    input_tokens,
    output_tokens,
    input_rate,
    output_rate,
    cached_tokens=0,
    cached_input_rate=None,
):
    """Estimate token-priced CNY cost; rates are CNY per million tokens."""
    incoming, outgoing, cached = map(
        _tokens, (input_tokens, output_tokens, cached_tokens)
    )
    if cached > incoming:
        raise ValueError("Cached input exceeds total input tokens.")
    if cached and cached_input_rate is None:
        raise ValueError("Cached input requires a verified cached-input rate.")
    return (
        (incoming - cached) * Decimal(str(input_rate))
        + outgoing * Decimal(str(output_rate))
        + cached * Decimal(str(cached_input_rate or 0))
    ) / Decimal(1_000_000)


def afp_cost(input_tokens, output_tokens, model_rates, monthly_price_cny, monthly_afp):
    """Estimate AFP and full-quota subscription allocation; never claim invoice cost."""
    if Decimal(str(monthly_afp)) <= 0 or Decimal(str(monthly_price_cny)) < 0:
        raise ValueError(
            "Subscription price must be nonnegative and AFP quota positive."
        )
    afp = (
        _tokens(input_tokens) * Decimal(str(model_rates["input_afp_coefficient"]))
        + _tokens(output_tokens) * Decimal(str(model_rates["output_afp_coefficient"]))
    ) / Decimal(10_000)
    return {
        "afp": afp,
        "allocated_cny": afp
        * Decimal(str(monthly_price_cny))
        / Decimal(str(monthly_afp)),
        "cost_basis": "subscription_allocation_at_full_monthly_quota",
        "actual_invoice_cny": None,
    }

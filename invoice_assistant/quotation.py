from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

from . import AppError


SUPPORTED_ROUNDING_UNITS = {100, 500, 1000}
SUPPORTED_VAT_MODES = {"inclusive", "direct"}
ONE = Decimal("1")
HUNDRED = Decimal("100")


def _decimal(value, label: str, *, minimum: Decimal | None = None, maximum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool) or value in (None, ""):
        raise AppError(f"请填写{label}。", 400, "invalid_quotation_input")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise AppError(f"{label}格式无效。", 400, "invalid_quotation_input")
    if not number.is_finite():
        raise AppError(f"{label}格式无效。", 400, "invalid_quotation_input")
    if minimum is not None and number < minimum:
        raise AppError(f"{label}不能小于 {minimum}。", 400, "invalid_quotation_input")
    if maximum is not None and number > maximum:
        raise AppError(f"{label}不能大于 {maximum}。", 400, "invalid_quotation_input")
    return number


def _integer(value, label: str, *, minimum: int, maximum: int) -> int:
    number = _decimal(value, label, minimum=Decimal(minimum), maximum=Decimal(maximum))
    integral = number.to_integral_value(rounding=ROUND_FLOOR)
    if number != integral:
        raise AppError(f"{label}必须是整数。", 400, "invalid_quotation_input")
    return int(integral)


def _percent(value, label: str) -> Decimal:
    return _decimal(value, label, minimum=Decimal("0"), maximum=Decimal("100")) / HUNDRED


def _round_yuan(value: Decimal) -> Decimal:
    return value.quantize(ONE, rounding=ROUND_HALF_UP)


def _truncate_yuan(value: Decimal) -> Decimal:
    return value.quantize(ONE, rounding=ROUND_FLOOR)


def _money_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate_quotation(payload: dict) -> dict:
    upper_limit = _decimal(
        payload.get("upper_limit"),
        "合同金额上限",
        minimum=Decimal("1"),
        maximum=Decimal("1000000000"),
    )
    vat_rate = _percent(payload.get("vat_rate", 3), "增值税率")
    surcharge_rate = _percent(payload.get("surcharge_rate", 12), "附加税率")
    management_rate = _percent(payload.get("management_rate", 16), "管理费比例")
    category_count = _integer(payload.get("category_count"), "大类条目数", minimum=1, maximum=100)
    rounding_unit = _integer(payload.get("rounding_unit", 100), "基础费用取整粒度", minimum=100, maximum=1000)
    if rounding_unit not in SUPPORTED_ROUNDING_UNITS:
        raise AppError("基础费用取整粒度仅支持 100、500 或 1000 元。", 400, "invalid_quotation_input")

    vat_mode = str(payload.get("vat_mode", "inclusive")).strip().lower()
    if vat_mode not in SUPPORTED_VAT_MODES:
        raise AppError("增值税计算口径无效。", 400, "invalid_quotation_input")

    vat_factor = vat_rate / (ONE + vat_rate) if vat_mode == "inclusive" else vat_rate
    estimated_basic_share = ONE - management_rate - vat_factor * (ONE + surcharge_rate)
    if estimated_basic_share <= Decimal("0.05"):
        raise AppError(
            "税费与管理费比例过高，留给基础开发费用的比例不足 5%。",
            400,
            "invalid_quotation_rates",
        )

    contract_cap = int(upper_limit.to_integral_value(rounding=ROUND_FLOOR))
    if contract_cap < category_count * rounding_unit:
        raise AppError(
            "金额上限不足以为每个大类分配一个取整单位，请提高上限、减少条目数或降低取整粒度。",
            400,
            "quotation_limit_too_low",
        )

    scan_distance = int(
        (Decimal(rounding_unit) / estimated_basic_share).to_integral_value(rounding=ROUND_CEILING)
    ) + rounding_unit + 10
    scan_distance = min(contract_cap - 1, scan_distance)

    solution = None
    for offset in range(scan_distance + 1):
        contract_total = Decimal(contract_cap - offset)
        vat_theoretical = (
            contract_total * vat_rate / (ONE + vat_rate)
            if vat_mode == "inclusive"
            else contract_total * vat_rate
        )
        vat = _round_yuan(vat_theoretical)
        surcharge_theoretical = vat * surcharge_rate
        surcharge = _round_yuan(surcharge_theoretical)
        management_theoretical = contract_total * management_rate
        management = _truncate_yuan(management_theoretical)
        basic_cost = contract_total - vat - surcharge - management

        if basic_cost <= 0 or basic_cost < category_count * rounding_unit:
            continue
        if int(basic_cost) % rounding_unit != 0:
            continue

        solution = {
            "contract_total": contract_total,
            "vat": vat,
            "vat_theoretical": vat_theoretical,
            "surcharge": surcharge,
            "surcharge_theoretical": surcharge_theoretical,
            "management": management,
            "management_theoretical": management_theoretical,
            "basic_cost": basic_cost,
        }
        break

    if solution is None:
        raise AppError(
            "未能在金额上限附近找到满足当前取整粒度的方案，请降低取整粒度或调整比例。",
            422,
            "quotation_solution_not_found",
        )

    basic_units = int(solution["basic_cost"]) // rounding_unit
    base_units, higher_count = divmod(basic_units, category_count)
    item_amounts = [
        (base_units + (1 if index < higher_count else 0)) * rounding_unit
        for index in range(category_count)
    ]
    groups = [
        {"amount": amount, "count": count}
        for amount, count in sorted(Counter(item_amounts).items())
    ]
    other_cost = solution["vat"] + solution["surcharge"] + solution["management"]
    contract_total = solution["contract_total"]

    return {
        "upper_limit": _money_number(upper_limit),
        "contract_total": int(contract_total),
        "gap_to_limit": _money_number(upper_limit - contract_total),
        "basic_cost": int(solution["basic_cost"]),
        "other_cost": int(other_cost),
        "category_count": category_count,
        "rounding_unit": rounding_unit,
        "vat_mode": vat_mode,
        "rates": {
            "vat": _money_number(vat_rate * HUNDRED),
            "surcharge": _money_number(surcharge_rate * HUNDRED),
            "management": _money_number(management_rate * HUNDRED),
        },
        "fees": {
            "vat": int(solution["vat"]),
            "surcharge": int(solution["surcharge"]),
            "management": int(solution["management"]),
        },
        "theoretical": {
            "vat": _money_number(solution["vat_theoretical"]),
            "surcharge": _money_number(solution["surcharge_theoretical"]),
            "management": _money_number(solution["management_theoretical"]),
        },
        "items": [
            {"index": index + 1, "name": f"开发条目 {index + 1}", "amount": amount}
            for index, amount in enumerate(item_amounts)
        ],
        "groups": groups,
        "item_min": min(item_amounts),
        "item_max": max(item_amounts),
        "item_average": _money_number(solution["basic_cost"] / Decimal(category_count)),
    }

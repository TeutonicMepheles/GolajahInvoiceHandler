from __future__ import annotations


def calculate(client, **overrides):
    payload = {
        "upper_limit": 47500,
        "vat_rate": 3,
        "surcharge_rate": 12,
        "management_rate": 16,
        "category_count": 8,
        "rounding_unit": 100,
        "vat_mode": "inclusive",
        **overrides,
    }
    return client.post("/api/quotations/calculate", json=payload)


def test_quotation_matches_reference_inclusive_tax_calculation(client):
    response = calculate(client)
    assert response.status_code == 200, response.get_json()
    result = response.get_json()

    assert result["contract_total"] == 47438
    assert result["gap_to_limit"] == 62
    assert result["basic_cost"] == 38300
    assert result["fees"] == {"vat": 1382, "surcharge": 166, "management": 7590}
    assert result["other_cost"] == 9138
    assert sum(item["amount"] for item in result["items"]) == 38300
    assert len(result["items"]) == 8
    assert result["item_min"] == 4700
    assert result["item_max"] == 4800
    assert result["groups"] == [{"amount": 4700, "count": 1}, {"amount": 4800, "count": 7}]


def test_quotation_supports_direct_contract_total_tax_mode(client):
    response = calculate(client, vat_mode="direct")
    assert response.status_code == 200, response.get_json()
    result = response.get_json()

    assert result["contract_total"] == 47495
    assert result["gap_to_limit"] == 5
    assert result["basic_cost"] == 38300
    assert result["fees"] == {"vat": 1425, "surcharge": 171, "management": 7599}


def test_quotation_distributes_clean_costs_by_selected_unit(client):
    response = calculate(client, upper_limit=200000, category_count=5, rounding_unit=500)
    assert response.status_code == 200, response.get_json()
    result = response.get_json()

    assert result["contract_total"] <= 200000
    assert result["basic_cost"] % 500 == 0
    assert len(result["items"]) == 5
    assert all(item["amount"] % 500 == 0 for item in result["items"])
    assert result["item_max"] - result["item_min"] <= 500
    assert result["contract_total"] == result["basic_cost"] + result["other_cost"]


def test_quotation_rejects_invalid_inputs(client):
    for overrides, error in (
        ({"category_count": 0}, "invalid_quotation_input"),
        ({"rounding_unit": 250}, "invalid_quotation_input"),
        ({"vat_mode": "unknown"}, "invalid_quotation_input"),
        ({"upper_limit": "NaN"}, "invalid_quotation_input"),
        ({"management_rate": 96}, "invalid_quotation_rates"),
        ({"upper_limit": 500, "category_count": 8}, "quotation_limit_too_low"),
    ):
        response = calculate(client, **overrides)
        assert response.status_code >= 400
        assert response.get_json()["error"] == error

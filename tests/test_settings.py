from __future__ import annotations

from pathlib import Path

from .conftest import confirm_item


def test_project_and_archive_settings(client, tmp_path):
    project = client.post("/api/projects", json={"name": "科研项目 A", "code": "RA-01", "notes": "横向经费"})
    assert project.status_code == 201
    project_id = project.get_json()["project"]["id"]
    updated = client.patch(f"/api/projects/{project_id}", json={"enabled": False, "notes": "已结束"})
    assert updated.status_code == 200
    assert updated.get_json()["project"]["enabled"] == 0

    relative = client.put("/api/settings/archive-root", json={"archive_root": "relative/path"})
    assert relative.status_code == 400
    target = tmp_path / "custom-archives"
    saved = client.put("/api/settings/archive-root", json={"archive_root": str(target.resolve())})
    assert saved.status_code == 200
    assert Path(saved.get_json()["archive_root"]).is_dir()


def test_projects_keep_one_active_and_reject_duplicate_names(client):
    last_active = client.patch("/api/projects/1", json={"enabled": False})
    assert last_active.status_code == 409
    assert last_active.get_json()["error"] == "last_active_project"

    created = client.post("/api/projects", json={"name": "项目 B"})
    assert created.status_code == 201
    project_id = created.get_json()["project"]["id"]
    duplicate = client.patch(f"/api/projects/{project_id}", json={"name": "默认报销项目"})
    assert duplicate.status_code == 409
    assert duplicate.get_json()["error"] == "project_exists"


def test_material_rules_require_contiguous_coverage(client):
    requirements_version = client.get("/api/settings").get_json()["requirements_version"]
    invalid = client.put(
        "/api/material-rules",
        json={"expected_requirements_version": requirements_version, "rules": [
            {"label": "0-500", "min_amount": 0, "max_amount": 500, "required": ["primary_receipt"]},
            {"label": "600+", "min_amount": 600, "max_amount": None, "required": ["primary_receipt"]},
        ]},
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["error"] == "rule_coverage_gap"

    valid = client.put(
        "/api/material-rules",
        json={"expected_requirements_version": requirements_version, "rules": [
            {"label": "低额", "min_amount": 0, "max_amount": 800, "required": ["primary_receipt"]},
            {"label": "高额", "min_amount": 800, "max_amount": None, "required": ["primary_receipt", "payment_record"]},
        ]},
    )
    assert valid.status_code == 200
    assert valid.get_json()["rules"][1]["required"] == ["primary_receipt", "payment_record"]


def test_material_names_are_configurable_data(client):
    response = client.put(
        "/api/material-labels",
        json={"materials": [
            {"code": "primary_receipt", "label": "核心票据"},
            {"code": "purchase_list", "label": "采购明细"},
            {"code": "payment_record", "label": "付款证明"},
        ]},
    )
    assert response.status_code == 200
    assert [entry["label"] for entry in response.get_json()["materials"]] == ["核心票据", "采购明细", "付款证明"]

    item = client.post(
        "/api/items/manual",
        json={"merchant": "材料名称测试", "expense_date": "2026-08-09", "amount": 1200, "currency": "CNY", "purpose": "测试", "project_id": 1},
    ).get_json()["item"]
    confirmed = confirm_item(client, item["id"]).get_json()["item"]
    assert [entry["label"] for entry in confirmed["material"]["missing"]] == ["核心票据", "采购明细", "付款证明"]

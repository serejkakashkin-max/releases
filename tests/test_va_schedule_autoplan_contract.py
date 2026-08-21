from VA.schedule_manager.services.autoplan_contract import AUTOPLAN_CONTRACT, autoplan_rule_ids


def test_autoplan_contract_has_unique_rule_ids():
    rule_ids = autoplan_rule_ids()

    assert len(rule_ids) == len(set(rule_ids))


def test_autoplan_contract_keeps_weekend_shift_exclusions_explicit():
    assert set(AUTOPLAN_CONTRACT.weekend_excluded_shift_codes) == {
        "ХД",
        "ДД",
        "ДР",
        "ВД",
        "ВР",
        "ХР",
        "8",
    }


def test_autoplan_contract_documents_core_policy():
    assert AUTOPLAN_CONTRACT.source_data_file == "schedule_data.json"
    assert AUTOPLAN_CONTRACT.historical_load_months == 3
    assert AUTOPLAN_CONTRACT.holiday_work_code == "ВХ"
    assert "manual.keep-existing-cells" in autoplan_rule_ids()
    assert "artifact.cell-level-explanations" in autoplan_rule_ids()

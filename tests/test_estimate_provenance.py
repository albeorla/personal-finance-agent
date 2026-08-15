"""Estimates carry their derivation as data: method, inputs, months, and gaps."""

import sqlite3

from financial_agent.config import ensure_source_tables
from financial_agent.obligations import (
    apply_obligation_instances,
    list_estimate_provenance,
    list_note_contradictions,
    list_obligation_review_candidates,
    list_obligations,
)
from financial_agent.schema import ensure_app_schema
from financial_agent.validate import build_validation_report

# The replayed case: 7 electric charges spread over 6 months, with nothing in
# 2026-01 and nothing since May, feeding a 2026-07 estimate.
EVIDENCE_DATES = [
    "2025-11-08",
    "2025-12-09",
    "2026-02-07",
    "2026-02-24",
    "2026-03-09",
    "2026-04-08",
    "2026-05-08",
]


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_source_tables(conn)
    ensure_app_schema(conn)
    conn.execute(
        "INSERT INTO accounts (id,name,org,kind,currency,first_seen_at,last_seen_at) "
        "VALUES ('chk','PREMIER PLUS CKG (4321)','Chase','checking','USD','x','x')"
    )
    return conn


def _seed_electric_candidate(conn, candidate_id="cand_electric"):
    for i, posted in enumerate(EVIDENCE_DATES):
        conn.execute(
            "INSERT INTO transactions (id,account_id,posted,amount,payee,source,"
            "first_seen_at,last_seen_at,fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"t{i}", "chk", f"{posted}T08:00:00", -115.87, "Eversource Energy",
             "simplefin", "x", "x", "x"),
        )
    conn.execute(
        "INSERT INTO charge_onboarding_candidates "
        "(id,merchant_key,display_name,direction,status,evidence_count,"
        " evidence_transaction_ids_json,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (candidate_id, "eversource", "Eversource Energy", "outflow", "proposed",
         len(EVIDENCE_DATES),
         '["t0","t1","t2","t3","t4","t5","t6"]', "x", "x"),
    )
    return f"charge_onboarding:{candidate_id}"


_DEFAULT_INPUTS = object()


def _apply_electric(conn, *, source, estimation_method="seasonal_multiplier",
                    estimation_inputs=_DEFAULT_INPUTS, notes=None, cadence="monthly",
                    review_after="2026-06-01"):
    apply_obligation_instances(
        conn,
        obligation={"id": "electric", "name": "Eversource Energy", "kind": "bill",
                    "cadence": cadence, "status": "active", "source": source},
        instances=[{
            "id": "electric:2026-07-15",
            "due_date": "2026-07-15",
            "amount": -168.0,
            "direction": "outflow",
            "status": "expected",
            "source": source,
            "confidence": "low",
            "notes": notes,
            "amount_status": "estimated",
            "amount_source": estimation_method,
            "estimation_method": estimation_method,
            "estimation_inputs": estimation_inputs if estimation_inputs is not _DEFAULT_INPUTS else {
                "method": "seasonal_multiplier", "amount": 168.0, "base_average": 112.0,
            },
            "review_after": review_after,
        }],
    )
    conn.commit()


def test_estimate_names_its_months_and_its_gaps(tmp_path):
    conn = _db(tmp_path / "p.sqlite")
    source = _seed_electric_candidate(conn)
    _apply_electric(conn, source=source)

    rows = list_estimate_provenance(conn)
    assert len(rows) == 1
    prov = rows[0]["estimate_provenance"]

    assert prov["method"] == "seasonal_multiplier"
    assert prov["inputs"]["base_average"] == 112.0
    assert prov["source_months"] == [
        "2025-11", "2025-12", "2026-02", "2026-03", "2026-04", "2026-05",
    ]
    assert prov["months_covered"] == 6
    assert prov["evidence_count"] == 7
    # The span runs through the estimate's own due month, so a stale estimate
    # shows the recent months it has no evidence for.
    assert prov["missing_months"] == ["2026-01", "2026-06", "2026-07"]
    assert prov["derived_from"] == "charge_onboarding_evidence"
    assert prov["complete"] is True
    assert "7 evidence charges across 6 months" in prov["gaps"]
    assert "no evidence for 2026-01, 2026-06, 2026-07" in prov["gaps"]


def test_provenance_rides_with_the_number_in_the_review_view(tmp_path):
    conn = _db(tmp_path / "p.sqlite")
    source = _seed_electric_candidate(conn)
    _apply_electric(conn, source=source)

    candidate = list_obligation_review_candidates(conn, as_of_date="2026-06-02")[0]

    # Same dict as the dollar figure: the number and its derivation travel together.
    assert candidate["amount"] == 168.0
    assert candidate["estimate_provenance"]["source_months"][0] == "2025-11"
    assert candidate["estimate_provenance"]["missing_months"] == ["2026-01", "2026-06", "2026-07"]


def test_provenance_rides_with_the_number_in_the_per_bill_view(tmp_path):
    """A charge-onboarded estimate has no review_after, so the per-bill lookup is
    the only view a session opens for it when the bill drives a cash trough."""

    conn = _db(tmp_path / "p.sqlite")
    source = _seed_electric_candidate(conn)
    _apply_electric(conn, source=source, review_after=None)

    assert list_obligation_review_candidates(conn, as_of_date="2026-06-02") == []

    instance = list_obligations(conn, obligation_id="electric")[0]["instances"][0]
    assert instance["review_after"] is None
    assert instance["amount"] == 168.0
    assert instance["estimate_provenance"]["source_months"] == [
        "2025-11", "2025-12", "2026-02", "2026-03", "2026-04", "2026-05",
    ]
    assert instance["estimate_provenance"]["missing_months"] == [
        "2026-01", "2026-06", "2026-07",
    ]

    # The compact per-row shape stays compact.
    summary = list_obligations(conn, obligation_id="electric", instances_summary=True)
    assert "estimate_provenance" not in summary[0]["instances"][0]


def test_confirmed_amount_has_no_provenance_to_defend(tmp_path):
    conn = _db(tmp_path / "p.sqlite")
    apply_obligation_instances(
        conn,
        obligation={"id": "rent", "name": "Rent", "kind": "bill", "cadence": "monthly",
                    "status": "active", "source": "obligations_yaml_manual"},
        instances=[{
            "id": "rent:2026-07-01",
            "due_date": "2026-07-01",
            "amount": -2100.0,
            "direction": "outflow",
            "status": "expected",
            "source": "obligations_yaml_manual",
            "amount_status": "confirmed",
        }],
    )
    conn.commit()

    instance = list_obligations(conn, obligation_id="rent")[0]["instances"][0]
    assert instance["estimate_provenance"] is None


def test_estimate_declaring_its_own_months_is_trusted_over_evidence(tmp_path):
    conn = _db(tmp_path / "p.sqlite")
    _apply_electric(
        conn,
        source="obligations_yaml_manual",
        estimation_inputs={"method": "average", "amount": 168.0,
                           "source_months": ["2026-04", "2026-05"], "evidence_count": 2},
    )

    prov = list_estimate_provenance(conn)[0]["estimate_provenance"]
    assert prov["derived_from"] == "estimation_inputs"
    assert prov["source_months"] == ["2026-04", "2026-05"]
    assert prov["missing_months"] == ["2026-06", "2026-07"]
    assert prov["complete"] is True


def test_null_provenance_is_reported_not_assumed(tmp_path):
    conn = _db(tmp_path / "p.sqlite")
    _apply_electric(conn, source="obligations_yaml_manual",
                    estimation_method=None, estimation_inputs=None)

    incomplete = list_estimate_provenance(conn, incomplete_only=True)
    assert [r["instance_id"] for r in incomplete] == ["electric:2026-07-15"]
    prov = incomplete[0]["estimate_provenance"]
    assert prov["complete"] is False
    assert prov["source_months"] == []
    assert prov["gaps"] == [
        "no estimation method recorded",
        "no estimation inputs recorded",
        "no source months recorded",
    ]

    report = build_validation_report(conn, as_of_date="2026-06-21")
    check = next(c for c in report["checks"] if c["name"] == "estimated_amounts_carry_provenance")
    assert check["passed"] is False
    assert check["evidence"]["instances"][0]["instance_id"] == "electric:2026-07-15"
    assert report["all_checks_passed"] is False


def test_complete_provenance_passes_validation(tmp_path):
    conn = _db(tmp_path / "p.sqlite")
    source = _seed_electric_candidate(conn)
    _apply_electric(conn, source=source)

    report = build_validation_report(conn, as_of_date="2026-06-21")
    check = next(c for c in report["checks"] if c["name"] == "estimated_amounts_carry_provenance")
    assert check["passed"] is True
    assert check["evidence"]["unprovenanced_count"] == 0


def test_note_contradicting_the_schedule_is_flagged(tmp_path):
    conn = _db(tmp_path / "p.sqlite")
    source = _seed_electric_candidate(conn)
    _apply_electric(conn, source=source, cadence="monthly",
                    notes="Utility switched us to quarterly billing in June.")

    flagged = list_note_contradictions(conn)
    assert [(f["field"], f["note_says"], f["record_says"]) for f in flagged] == [
        ("cadence", "quarterly", "monthly")
    ]
    assert flagged[0]["instance_id"] == "electric:2026-07-15"

    report = build_validation_report(conn, as_of_date="2026-06-21")
    check = next(c for c in report["checks"] if c["name"] == "notes_agree_with_records")
    assert check["passed"] is False
    assert check["evidence"]["instances"][0]["note_says"] == "quarterly"


def test_note_saying_the_charge_stopped_is_flagged_while_the_row_still_projects(tmp_path):
    conn = _db(tmp_path / "p.sqlite")
    source = _seed_electric_candidate(conn)
    _apply_electric(conn, source=source, notes="Account closed after the move; keeping for history.")

    flagged = list_note_contradictions(conn)
    assert [(f["field"], f["note_says"], f["record_says"]) for f in flagged] == [
        ("status", "closed", "expected")
    ]


def test_note_agreeing_with_the_record_is_not_flagged(tmp_path):
    conn = _db(tmp_path / "p.sqlite")
    source = _seed_electric_candidate(conn)
    _apply_electric(conn, source=source, cadence="annual",
                    notes="Billed yearly; 7 observed charges, monthly-average amount policy is not used here.")

    # 'yearly' and 'annual' are the same cadence, so only the real disagreement
    # ('monthly') comes back.
    assert [(f["field"], f["note_says"]) for f in list_note_contradictions(conn)] == [
        ("cadence", "monthly")
    ]

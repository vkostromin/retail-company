import pandas as pd

from retail_agent.safety.pii import redact_text, sanitize_dataframe


def test_redacts_common_pii():
    text = (
        "Top customer john.doe@example.com lives at 1234 Maple Grove Street, call (415) 555-0132, "
        "located at 37.774929, -122.419416 POINT(-122.4 37.7)"
    )
    out, counts = redact_text(text)
    assert "example.com" not in out and "555-0132" not in out and "Maple Grove" not in out
    assert "37.774929" not in out and "POINT(" not in out
    assert set(counts) == {"email", "phone", "street_address", "lat_long", "geo_point"}


def test_does_not_redact_business_numbers():
    text = "Revenue was $1,234,567.89 in 2024-03 across 12,345 orders; user 48213 spent 2049.5. AOV 85.23, 12.5% growth"
    out, counts = redact_text(text)
    assert out == text and not counts


def test_sanitize_dataframe_drops_pii_columns_and_redacts_cells():
    df = pd.DataFrame({"user_id": [1], "contact_email": ["a@b.com"], "note": ["mail me at x@y.org"], "rev": [10.0]})
    out, counts = sanitize_dataframe(df)
    assert list(out.columns) == ["user_id", "note", "rev"]
    assert "x@y.org" not in out.loc[0, "note"]
    assert counts["pii_column"] == 1 and counts["email"] == 1

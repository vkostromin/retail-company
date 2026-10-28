import pytest

from retail_agent.safety.entitlements import Principal, ProductScope

DATASET = "bigquery-public-data.thelook_ecommerce"


@pytest.fixture
def ceo():
    return Principal(user_id="ceo", display_name="CEO", role="admin", scope=ProductScope(all_products=True))


@pytest.fixture
def alice():
    return Principal(user_id="alice", display_name="Alice", scope=ProductScope(departments=["Women"]))

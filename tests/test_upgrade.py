import pytest
from sqlalchemy import inspect, text


def test_crawler_blocks_redirect_into_private_network(net):
    from app.services import crawler

    with pytest.raises(ValueError, match="non-public"):
        crawler.fetch("https://redirector.test/")
    with pytest.raises(ValueError, match="non-public"):
        crawler.fetch("http://127.0.0.1:5432/")
    assert not any(r.url.host == "169.254.169.254" for r in net.requests)


def test_additive_upgrade_adds_new_columns(net):
    """An older database (missing a column) upgrades in place on boot."""
    from app import db

    db.init_db()
    eng = db.engine()
    with eng.begin() as conn:
        if eng.dialect.name == "sqlite":
            conn.execute(text("ALTER TABLE calls DROP COLUMN flag_reason"))
        else:
            conn.execute(text('ALTER TABLE calls DROP COLUMN "flag_reason"'))
    assert "flag_reason" not in {c["name"] for c in inspect(eng).get_columns("calls")}
    added = db.add_missing_columns()
    assert "calls.flag_reason" in added
    assert "flag_reason" in {c["name"] for c in inspect(db.engine()).get_columns("calls")}

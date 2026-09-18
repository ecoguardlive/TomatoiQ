from datetime import datetime, timezone

from persistence import TomatoRepository


def state(total=3, ready=1, risk=0, timestamp=None):
    return {
        "updated_at": timestamp or datetime.now(timezone.utc).isoformat(),
        "total_tomatoes": total,
        "ready_now": ready,
        "disease_suspect_count": risk,
        "counts_by_class": {"green": 1, "fully_ripened": ready},
    }


def test_snapshot_persistence_and_time_window(tmp_path):
    repo = TomatoRepository(tmp_path / "tomatoiq.db")
    assert repo.append_snapshot(state()) is True
    # A rapid repeat is intentionally coalesced, avoiding database growth.
    assert repo.append_snapshot(state()) is False
    points = repo.snapshots_since(datetime.now(timezone.utc).replace(year=2000))
    assert len(points) == 1
    assert points[0]["total_tomatoes"] == 3


def test_alert_lifecycle(tmp_path):
    repo = TomatoRepository(tmp_path / "tomatoiq.db")
    alert_id = repo.create_alert(tomato_id=7, severity="warning", category="inspection", message="Inspect tomato 7")
    assert repo.list_alerts()[0]["id"] == alert_id
    assert repo.resolve_alert(alert_id) is True
    assert repo.list_alerts() == []
    assert repo.resolve_alert(alert_id) is False

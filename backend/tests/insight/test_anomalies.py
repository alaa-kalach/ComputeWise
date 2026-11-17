from app.agent.tools.anomalies import detect_spend_anomalies


def test_spike_is_flagged():
    data = {"key-1": [0.5, 0.6, 0.5, 0.6, 0.5, 0.6, 0.5, 6.0]}
    out = detect_spend_anomalies(data)
    assert len(out) == 1
    assert out[0]["resource_id"] == "key-1"
    assert out[0]["latest_usd"] == 6.0


def test_steady_resource_not_flagged():
    data = {"key-1": [1.0, 1.1, 0.9, 1.0, 1.2, 1.0, 1.1, 1.3]}
    assert detect_spend_anomalies(data) == []


def test_tiny_jump_ignored():
    # 10x jump, but only $0.18 -> not worth a human's attention
    data = {"key-1": [0.02] * 7 + [0.20]}
    assert detect_spend_anomalies(data) == []


def test_short_history_skipped():
    assert detect_spend_anomalies({"key-1": [0.1, 9.0]}) == []


def test_biggest_jump_first():
    data = {
        "a": [1.0] * 7 + [5.0],
        "b": [1.0] * 7 + [12.0],
    }
    out = detect_spend_anomalies(data)
    assert [x["resource_id"] for x in out] == ["b", "a"]

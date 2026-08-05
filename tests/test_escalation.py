"""Tests for the escalation module."""
import pytest
from sensor_bridge.normalizer import SensorReading
from sensor_bridge.pattern_detector import PatternEvent, PatternType, Severity
from sensor_bridge.escalation import EscalationRouter, EscalationAction, EscalationLevel


@pytest.fixture
def escalation_config():
    return {
        "level_0_normal": {"notify": [], "log": True, "page_laforge": False},
        "level_1_warning": {
            "notify": [], "log": True, "page_laforge": False,
            "laforge_review_on_next": True,
        },
        "level_2_alert": {
            "notify": ["captain"], "log": True, "page_laforge": True,
            "laforge_priority": "normal",
        },
        "level_3_critical": {
            "notify": ["captain", "crew"], "log": True, "page_laforge": True,
            "laforge_priority": "urgent",
        },
    }


@pytest.fixture
def router(escalation_config, tmp_path):
    return EscalationRouter(
        escalation_config=escalation_config,
        log_dir=str(tmp_path / "escalation_logs"),
    )


def make_pattern(severity, ptype=PatternType.THRESHOLD_WARNING, device="engine_1", sensor="temp"):
    return PatternEvent(
        device_id=device, sensor=sensor,
        pattern_type=ptype, severity=severity,
        value=95.0, message="Test pattern",
    )


class TestEscalationLevelMapping:
    def test_normal_severity_maps_to_level_0(self, router):
        action = router.route(make_pattern(Severity.NORMAL, PatternType.RECOVERY))
        assert action.level == EscalationLevel.NORMAL

    def test_warning_severity_maps_to_level_1(self, router):
        action = router.route(make_pattern(Severity.WARNING))
        assert action.level == EscalationLevel.WARNING

    def test_alert_severity_maps_to_level_2(self, router):
        action = router.route(make_pattern(Severity.ALERT))
        assert action.level == EscalationLevel.ALERT

    def test_critical_severity_maps_to_level_3(self, router):
        action = router.route(make_pattern(Severity.CRITICAL))
        assert action.level == EscalationLevel.CRITICAL


class TestEscalationActions:
    def test_level_0_no_notification(self, router):
        action = router.route(make_pattern(Severity.NORMAL, PatternType.RECOVERY))
        assert action.notify == []
        assert action.page_laforge is False

    def test_level_1_logs_for_review(self, router):
        action = router.route(make_pattern(Severity.WARNING))
        assert action.log_for_review is True
        assert action.page_laforge is False

    def test_level_2_notifies_captain(self, router):
        action = router.route(make_pattern(Severity.ALERT))
        assert "captain" in action.notify
        assert action.page_laforge is True

    def test_level_3_all_hands(self, router):
        action = router.route(make_pattern(Severity.CRITICAL))
        assert "captain" in action.notify
        assert "crew" in action.notify
        assert action.laforge_priority == "urgent"

    def test_action_has_message(self, router):
        action = router.route(make_pattern(Severity.WARNING))
        assert len(action.actions) > 0
        assert any("Warning" in a for a in action.actions)


class TestNotificationCallbacks:
    def test_callback_fired(self, router, escalation_config):
        received = []
        router.register_notifier("captain", lambda action: received.append(action))

        router.route(make_pattern(Severity.ALERT))
        assert len(received) == 1
        assert received[0].level == EscalationLevel.ALERT

    def test_callback_not_fired_for_level_0(self, router):
        received = []
        router.register_notifier("captain", lambda action: received.append(action))

        router.route(make_pattern(Severity.NORMAL, PatternType.RECOVERY))
        assert len(received) == 0


class TestCooldown:
    def test_cooldown_prevents_repeated_escalation(self, router):
        """Same sensor escalating twice within cooldown → suppressed."""
        action1 = router.route(make_pattern(Severity.ALERT, device="d", sensor="s"))
        assert action1.page_laforge is True

        action2 = router.route(make_pattern(Severity.ALERT, device="d", sensor="s"))
        # Cooldown should suppress
        assert action2.page_laforge is False
        assert action2.notify == []

    def test_cooldown_different_sensors(self, router):
        """Different sensors don't share cooldown."""
        router.route(make_pattern(Severity.ALERT, device="d", sensor="s1"))
        action2 = router.route(make_pattern(Severity.ALERT, device="d", sensor="s2"))
        assert action2.page_laforge is True


class TestPendingReviews:
    def test_level_1_accumulates(self, router):
        router.route(make_pattern(Severity.WARNING, device="d1", sensor="s1"))
        router.route(make_pattern(Severity.WARNING, device="d2", sensor="s2"))

        reviews = router.get_pending_reviews()
        assert len(reviews) == 2

    def test_get_pending_reviews_clears(self, router):
        router.route(make_pattern(Severity.WARNING))
        assert len(router.get_pending_reviews()) == 1
        # Second call should be empty
        assert len(router.get_pending_reviews()) == 0


class TestEscalationAction:
    def test_level_name(self):
        action = EscalationAction(
            level=EscalationLevel.CRITICAL,
            pattern=make_pattern(Severity.CRITICAL),
        )
        assert action.level_name == "CRITICAL"

    def test_to_dict(self):
        action = EscalationAction(
            level=EscalationLevel.WARNING,
            pattern=make_pattern(Severity.WARNING),
        )
        d = action.to_dict()
        assert d["level"] == 1
        assert d["level_name"] == "WARNING"
        assert "pattern" in d

    def test_to_json(self):
        action = EscalationAction(
            level=EscalationLevel.ALERT,
            pattern=make_pattern(Severity.ALERT),
        )
        s = action.to_json()
        assert isinstance(s, str)
        assert '"level": 2' in s

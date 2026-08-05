"""
Escalation — decides when to page LaForge, notify the captain, or stay quiet.

The escalation protocol has 4 levels, mirroring the two-agent architecture:

    Level 0 (Normal):
        The ensign handles everything. No notification. Data flows into
        history and the pattern detector watches. Life is good.

    Level 1 (Warning):
        The ensign still handles it, but the event is logged for LaForge's
        next review. LaForge isn't paged — he'll see it when he wakes up
        for routine maintenance. This is "the logbook note that the chief
        engineer reads over coffee."

    Level 2 (Alert):
        The ensign handles the immediate response (e.g., sounding the buzzer),
        but the captain is notified immediately, and LaForge is paged for a
        non-urgent review. "Chief, we had a temp spike. Look at it when you
        get a chance, but I've got it under control."

    Level 3 (Critical):
        All hands notified. LaForge is invoked with urgent priority. The ensign
        executes its critical procedures (shutdown, alarm, etc.) but the
        architect needs to look at this NOW. "Chief, get down here."

The escalation module is stateless — it receives PatternEvents and routes
them according to the configured escalation policy. It doesn't decide IF
something is wrong (that's the pattern detector's job). It decides WHO
needs to know about it.

Design principle (from TWO_AGENTS_NOT_ONE):
    "You don't wake LaForge for every sensor reading. You wake him when
    the ensign can't handle it." The escalation module is the gatekeeper
    for LaForge's attention — the most expensive resource in the system.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable

from .pattern_detector import PatternEvent, Severity

logger = logging.getLogger(__name__)


class EscalationLevel(IntEnum):
    """The 4-level escalation protocol."""
    NORMAL = 0    # Level 0 — ensign handles, no notification
    WARNING = 1   # Level 1 — ensign handles, logs for LaForge review
    ALERT = 2     # Level 2 — ensign handles, notifies captain, pages LaForge
    CRITICAL = 3  # Level 3 — all hands notified, LaForge invoked urgently


@dataclass
class EscalationAction:
    """The result of an escalation decision — what should happen."""
    level: EscalationLevel
    pattern: PatternEvent
    actions: list[str] = field(default_factory=list)  # Human-readable action items
    notify: list[str] = field(default_factory=list)   # Who to notify
    page_laforge: bool = False
    laforge_priority: str = "normal"  # normal | urgent
    log_for_review: bool = False      # True = LaForge should review on next wake
    timestamp: float = field(default_factory=time.time)

    @property
    def level_name(self) -> str:
        names = {
            0: "NORMAL",
            1: "WARNING",
            2: "ALERT",
            3: "CRITICAL",
        }
        return names.get(int(self.level), "UNKNOWN")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["level"] = int(self.level)
        d["level_name"] = self.level_name
        d["pattern"] = self.pattern.to_dict()
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


class EscalationRouter:
    """
    Routes PatternEvents to the appropriate escalation level.

    The router maintains a log of escalation events for LaForge's review
    and provides callbacks for notifications.
    """

    def __init__(
        self,
        escalation_config: dict[str, Any],
        log_dir: str = "data/escalation_logs",
    ):
        """
        Args:
            escalation_config: The 'escalation' section from config.yaml.
            log_dir: Directory for escalation log files.
        """
        self.config = escalation_config
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Notification callbacks: {channel_name: callback_fn}
        self._notify_callbacks: dict[str, Callable[[EscalationAction], None]] = {}

        # Pending reviews for LaForge (Level 1 events that accumulate)
        self._pending_reviews: list[EscalationAction] = []

        # Cooldown tracking: prevent repeated escalations for the same sensor
        # {(device, sensor): last_escalation_timestamp}
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._cooldown_seconds = 300  # 5 minutes between same-sensor escalations

        # Current log file (one per day)
        self._init_log_file()

    def _init_log_file(self) -> None:
        date_str = time.strftime("%Y-%m-%d", time.gmtime())
        self._log_path = self.log_dir / f"escalation_{date_str}.jsonl"

    def register_notifier(
        self,
        channel: str,
        callback: Callable[[EscalationAction], None],
    ) -> None:
        """Register a notification callback for a channel (e.g., 'captain')."""
        self._notify_callbacks[channel] = callback
        logger.info("Registered notifier for channel: %s", channel)

    def route(self, pattern: PatternEvent) -> EscalationAction:
        """
        Route a PatternEvent through the escalation protocol.

        Maps the pattern's Severity to an EscalationLevel, applies cooldown
        logic, and returns an EscalationAction with all routing decisions.

        Args:
            pattern: The detected pattern from PatternDetector.

        Returns:
            EscalationAction with routing decisions.
        """
        level = self._severity_to_level(pattern.severity)
        action = self._build_action(level, pattern)

        # Cooldown check: don't re-escalate the same sensor repeatedly
        key = (pattern.device_id, pattern.sensor)
        now = time.time()
        if level >= EscalationLevel.ALERT:
            last = self._cooldowns.get(key, 0)
            if now - last < self._cooldown_seconds and pattern.pattern_type.name != "RECOVERY":
                # In cooldown — downgrade to just logging
                logger.debug(
                    "Cooldown active for %s/%s — logging only",
                    pattern.device_id, pattern.sensor,
                )
                action.notify = []
                action.page_laforge = False
        else:
            # Normal/recovery clears the cooldown
            self._cooldowns.pop(key, None)

        if level >= EscalationLevel.ALERT:
            self._cooldowns[key] = now

        # Execute actions
        self._execute(action)

        return action

    def _severity_to_level(self, severity: Severity) -> EscalationLevel:
        """Map PatternDetector.Severity to EscalationLevel."""
        if severity >= Severity.CRITICAL:
            return EscalationLevel.CRITICAL
        elif severity >= Severity.ALERT:
            return EscalationLevel.ALERT
        elif severity >= Severity.WARNING:
            return EscalationLevel.WARNING
        return EscalationLevel.NORMAL

    def _build_action(
        self,
        level: EscalationLevel,
        pattern: PatternEvent,
    ) -> EscalationAction:
        """Build an EscalationAction from level + pattern."""
        # Look up config for this level
        config_key = f"level_{int(level)}_{'normal' if level == 0 else 'warning' if level == 1 else 'alert' if level == 2 else 'critical'}"
        level_config = self.config.get(config_key, {})

        notify = list(level_config.get("notify", []))
        page_laforge = level_config.get("page_laforge", False)
        laforge_priority = level_config.get("laforge_priority", "normal")
        log_for_review = level_config.get("laforge_review_on_next", False)

        actions: list[str] = []

        if level == EscalationLevel.NORMAL:
            if pattern.pattern_type.name == "RECOVERY":
                actions.append(f"Log recovery: {pattern.message}")
            else:
                actions.append(f"Normal reading: {pattern.value:.1f}")

        elif level == EscalationLevel.WARNING:
            actions.append(f"Warning logged: {pattern.message}")
            if log_for_review:
                actions.append("Flagged for LaForge's next review")

        elif level == EscalationLevel.ALERT:
            actions.append(f"Alert: {pattern.message}")
            actions.append("Captain notified")
            if page_laforge:
                actions.append(f"LaForge paged (priority: {laforge_priority})")

        elif level == EscalationLevel.CRITICAL:
            actions.append(f"CRITICAL: {pattern.message}")
            actions.append("All hands notified")
            actions.append(f"LaForge invoked (priority: {laforge_priority})")

        action = EscalationAction(
            level=level,
            pattern=pattern,
            actions=actions,
            notify=notify,
            page_laforge=page_laforge,
            laforge_priority=laforge_priority,
            log_for_review=log_for_review,
        )

        # Accumulate Level 1 for LaForge's review
        if log_for_review:
            self._pending_reviews.append(action)

        return action

    def _execute(self, action: EscalationAction) -> None:
        """Execute the routing: log, notify, page."""
        # Always log
        self._log(action)

        # Send notifications
        for channel in action.notify:
            callback = self._notify_callbacks.get(channel)
            if callback:
                try:
                    callback(action)
                except Exception:
                    logger.exception(
                        "Notification callback failed for channel: %s", channel
                    )
            else:
                logger.debug("No callback registered for channel: %s", channel)

    def _log(self, action: EscalationAction) -> None:
        """Append the escalation to the daily log file."""
        # Check if we need to roll over to a new day's file
        self._init_log_file()

        try:
            with open(self._log_path, "a") as f:
                f.write(action.to_json() + "\n")
        except OSError:
            logger.exception("Failed to write escalation log")

    def get_pending_reviews(self) -> list[EscalationAction]:
        """
        Get accumulated Level 1 warnings for LaForge's review.

        Called when LaForge wakes up. Returns the list and clears it.
        """
        reviews = self._pending_reviews
        self._pending_reviews = []
        return reviews

    def get_escalation_summary(self) -> dict[str, Any]:
        """Get a summary of recent escalations (for status/dashboard)."""
        return {
            "pending_reviews": len(self._pending_reviews),
            "active_cooldowns": len(self._cooldowns),
            "cooldown_sensors": [
                {"device": d, "sensor": s}
                for d, s in self._cooldowns.keys()
            ],
            "log_file": str(self._log_path),
        }

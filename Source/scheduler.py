"""
Market-hours-aware scheduling engine for trading workspaces.

Provides WorkspaceScheduleManager (one per agent team) and
SchedulerOrchestrator (manages multiple workspaces).  Uses APScheduler
with CronTrigger/IntervalTrigger driven by MarketProfile YAML configs.

All job execution methods are market-hours-guarded: they check
``MarketProfile.is_market_open()`` and ``orchestrator_agent._trading_paused``
before running any work.  APScheduler coalescing and max_instances prevent
overlapping cycles.

Usage::

    from Source.scheduler import WorkspaceScheduleManager, SchedulerOrchestrator
    import yaml

    config = yaml.safe_load(open("Config/workspace_schedules/my_team.yaml"))
    mgr = WorkspaceScheduleManager(config_dict=config)
    mgr.start()

    # Multi-workspace
    orch = SchedulerOrchestrator()
    orch.load_all()
    orch.start_all()
"""

# Set trading mode for faster database discovery
import os
os.environ['JARVIS_TRADING_MODE'] = '1'

import asyncio
import logging
import os
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from zoneinfo import ZoneInfo

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
except ImportError as _imp_err:
    raise ImportError(
        "APScheduler is required for the scheduling engine. "
        "Install it with: pip install apscheduler>=3.10"
    ) from _imp_err

from Source.market_profile import MarketProfile

logger = logging.getLogger("trading.scheduler")

# Default workspace schedules directory relative to this file
_DEFAULT_SCHEDULES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "Config",
    "workspace_schedules",
)


# ======================================================================
# Weekend State Machine
# ======================================================================


class WeekendManager:
    """State machine managing market open/close transitions for a workspace.

    States:
        - ``TRADING``: Market is open, all jobs running normally.
        - ``WEEKEND_SHUTDOWN``: Market just closed, jobs being paused.
        - ``WEEKEND``: Market closed, waiting for next open.
        - ``MARKET_OPEN``: Market just opened, jobs being resumed.

    Continuous markets (crypto) should NOT use WeekendManager.
    """

    # State constants
    TRADING = "TRADING"
    WEEKEND_SHUTDOWN = "WEEKEND_SHUTDOWN"
    WEEKEND = "WEEKEND"
    MARKET_OPEN = "MARKET_OPEN"

    def __init__(
        self, profile: MarketProfile, manager: "WorkspaceScheduleManager"
    ) -> None:
        self._profile = profile
        self._manager = manager
        self._logger = logging.getLogger("trading.scheduler.weekend")

        # Determine initial state from current market hours
        if self._profile.is_market_open():
            self._state = self.TRADING
        elif self._profile.is_weekend():
            self._state = self.WEEKEND
        else:
            # Between close and next open (e.g., daily gap in futures)
            self._state = self.WEEKEND

        self._logger.info(
            "WeekendManager initialised: state=%s market_open=%s weekend=%s",
            self._state,
            self._profile.is_market_open(),
            self._profile.is_weekend(),
        )

    def get_state(self) -> str:
        """Return the current state."""
        return self._state

    def check_transition(self) -> None:
        """Check and perform state transitions based on current market hours.

        Called periodically (every minute) by a scheduler job.  Transitions:

        - TRADING -> WEEKEND_SHUTDOWN: market weekend detected
        - WEEKEND_SHUTDOWN -> WEEKEND: immediate after pause
        - WEEKEND -> MARKET_OPEN: market opens
        - MARKET_OPEN -> TRADING: after resume
        """
        if self._state == self.TRADING:
            if self._profile.is_weekend():
                self._logger.info("Market closing for weekend")
                self._manager.pause()
                self._state = self.WEEKEND_SHUTDOWN
                # Immediate transition to WEEKEND after pause
                self._logger.info(
                    "Weekend mode active. Next open: %s",
                    self._profile.next_open(),
                )
                self._state = self.WEEKEND

        elif self._state == self.WEEKEND:
            if self._profile.is_market_open():
                self._logger.info("Market opening")
                self._state = self.MARKET_OPEN
                # Resume and transition to TRADING
                self._manager.resume()
                self._logger.info("Trading resumed")
                self._state = self.TRADING

        elif self._state == self.WEEKEND_SHUTDOWN:
            # Should not stay here (immediate transition in TRADING branch)
            # but handle gracefully
            self._logger.info(
                "Weekend mode active. Next open: %s",
                self._profile.next_open(),
            )
            self._state = self.WEEKEND

        elif self._state == self.MARKET_OPEN:
            # Should not stay here (immediate transition in WEEKEND branch)
            # but handle gracefully
            self._manager.resume()
            self._logger.info("Trading resumed")
            self._state = self.TRADING

    def force_weekend(self) -> None:
        """Force transition to weekend mode regardless of current time.

        Useful for early Friday shutdown or manual intervention.
        """
        if self._state == self.TRADING:
            self._logger.info("Forced weekend shutdown")
            self._manager.pause()
        self._state = self.WEEKEND
        self._logger.info(
            "Forced weekend mode. Next open: %s",
            self._profile.next_open(),
        )

    def force_resume(self) -> None:
        """Force resume from weekend mode regardless of current time.

        Useful for testing or manual override.
        """
        self._state = self.MARKET_OPEN
        self._manager.resume()
        self._logger.info("Forced resume from weekend")
        self._state = self.TRADING


class WorkspaceScheduleManager:
    """Manages the APScheduler schedule for a single workspace (agent team).

    Loads a MarketProfile and workspace config, then creates cron/interval
    jobs for trading cycles, position monitoring, and news monitoring.
    All jobs are market-hours-guarded.

    Args:
        config_path: Path to a workspace schedule YAML file.
        config_dict: Workspace schedule config as a dict (alternative to path).
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        config_dict: Optional[dict] = None,
    ) -> None:
        if config_path:
            with open(config_path, "r") as f:
                self._config: Dict[str, Any] = yaml.safe_load(f)
        elif config_dict:
            self._config = dict(config_dict)
        else:
            raise ValueError("Provide config_path or config_dict")

        # Load market profile
        market_type = self._config["market_profile"]
        self._profile = MarketProfile.from_market_type(market_type)

        # Core settings
        self._instruments: List[str] = list(self._config.get("instruments", []))
        self._primary_timeframe: str = self._config.get("primary_timeframe", "H1")

        # Get schedule config from market profile, apply overrides
        self._schedule: Dict[str, Any] = self._profile.get_schedule_for_timeframe(
            self._primary_timeframe
        )
        overrides = self._config.get("schedule_override")
        if overrides and isinstance(overrides, dict):
            self._schedule.update(overrides)

        # Scheduler state
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._running: bool = False
        self._cycle: Any = None  # Lazy TradingCycle singleton
        self._weekend_manager: Optional[WeekendManager] = None

        team_name = self._config.get("team_name", "unknown")
        self._logger = logging.getLogger(f"trading.scheduler.{team_name}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Create and start the APScheduler with all configured jobs.

        Jobs use coalesce=True (merge missed fires), max_instances=1
        (no parallel runs), and misfire_grace_time=60s.
        """
        self._scheduler = AsyncIOScheduler(
            timezone=self._profile._tz,
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 60,
            },
        )

        self._add_cycle_job()

        pos_cfg = self._config.get("position_monitoring", {})
        if pos_cfg.get("enabled", True):
            self._add_position_monitor_job()

        news_cfg = self._config.get("news_monitoring", {})
        if news_cfg.get("enabled", True):
            self._add_news_monitor_job()

        # Daily report job (from market profile config)
        self._add_daily_report_job()

        # Weekly report job (Sundays 6PM ET)
        self._add_weekly_report_job()

        # Nightly flight recorder digest (23:55 ET every day)
        self._add_nightly_digest_job()

        # Nightly QA audit via Claude Code CLI (10 PM ET weeknights)
        self._add_nightly_qa_audit_job()

        # Nightly parameter optimizer (10:30 PM ET weeknights, after QA audit)
        self._add_nightly_optimizer_job()

        # Tuning measurement snapshots (every 6 hours)
        self._add_tuning_measurement_job()

        # Ghost-snipes nightly batch — record validator-snipe triggers + replay
        # outcomes via guardian-equivalent simulation (23:30 ET daily)
        self._add_ghost_snipes_job()

        # Weekly setup discovery — scan backtest_trades for new auto-discovered
        # S21+ patterns and append to custom_setups.json (Sat 8 AM ET, market closed)
        self._add_weekly_setup_discovery_job()

        # Weekend management (non-continuous markets only)
        if not self._profile.is_continuous:
            self._weekend_manager = WeekendManager(self._profile, self)
            self._add_weekend_check_job()

        self._scheduler.start()
        self._running = True

        job_count = len(self._scheduler.get_jobs())
        self._logger.info(
            "Scheduler started: team=%s profile=%s timeframe=%s jobs=%d",
            self._config.get("team_name", "?"),
            self._profile.market_type,
            self._primary_timeframe,
            job_count,
        )

    def stop(self) -> None:
        """Shutdown the scheduler (non-blocking)."""
        if self._scheduler and self._running:
            self._scheduler.shutdown(wait=False)
            self._running = False
            self._logger.info("Scheduler stopped")

    def pause(self) -> None:
        """Pause all scheduled jobs."""
        if self._scheduler:
            self._scheduler.pause()
            self._logger.info("Scheduler paused")

    def resume(self) -> None:
        """Resume all scheduled jobs."""
        if self._scheduler:
            self._scheduler.resume()
            self._logger.info("Scheduler resumed")

    @property
    def is_running(self) -> bool:
        """True if the scheduler is currently running."""
        return self._running

    def get_status(self) -> dict:
        """Return current scheduler status.

        Returns:
            Dict with workspace_id, team_name, market_profile, timeframe,
            instruments, running, paused, job_count, next_run_times.
        """
        jobs = self._scheduler.get_jobs() if self._scheduler else []
        next_runs = {}
        for job in jobs:
            next_fire = job.next_run_time
            next_runs[job.id] = (
                next_fire.isoformat() if next_fire else None
            )

        paused = False
        if self._scheduler:
            # APScheduler 3.x: check if any job has next_run_time == None
            # while scheduler is running (indicates paused state)
            paused = self._running and all(
                j.next_run_time is None for j in jobs
            ) and len(jobs) > 0

        return {
            "workspace_id": self._config.get("workspace_id", ""),
            "team_name": self._config.get("team_name", ""),
            "market_profile": self._profile.market_type,
            "timeframe": self._primary_timeframe,
            "instruments": self._instruments,
            "running": self._running,
            "paused": paused,
            "job_count": len(jobs),
            "next_run_times": next_runs,
            "weekend_state": self.get_weekend_state(),
        }

    # ------------------------------------------------------------------
    # Job registration (private)
    # ------------------------------------------------------------------

    def _add_cycle_job(self) -> None:
        """Add the trading cycle cron job based on timeframe schedule."""
        workspace_id = self._config.get("workspace_id", "ws")
        sched = self._schedule
        tz = self._profile._tz

        tf = self._primary_timeframe

        if tf == "D":
            # Daily: specific hour and minute
            trigger = CronTrigger(
                hour=str(sched.get("cron_hour", 17)),
                minute=str(sched.get("cron_minute", 5)),
                timezone=tz,
            )
        elif tf == "H4":
            # 4-hour: specific hours and minute
            trigger = CronTrigger(
                hour=str(sched.get("cron_hours", "1,5,9,13,17,21")),
                minute=str(sched.get("cron_minutes", "2")),
                timezone=tz,
            )
        else:
            # M15, H1, etc.: minute-based cron
            trigger = CronTrigger(
                minute=str(sched.get("cron_minutes", "0")),
                timezone=tz,
            )

        self._scheduler.add_job(
            self._execute_cycle,
            trigger,
            id=f"{workspace_id}_cycle",
            name=f"Trading cycle ({tf})",
        )

        self._logger.info(
            "Cycle job added: timeframe=%s trigger=%s", tf, trigger,
        )

    def _add_position_monitor_job(self) -> None:
        """Add the position monitoring interval job."""
        workspace_id = self._config.get("workspace_id", "ws")

        # Check for workspace-level override
        pos_cfg = self._config.get("position_monitoring", {})
        override = pos_cfg.get("override")
        if override:
            offset = int(override)
        else:
            offset = int(self._schedule.get("position_monitor_offset", 7))

        trigger = IntervalTrigger(
            minutes=offset, timezone=self._profile._tz,
        )

        self._scheduler.add_job(
            self._execute_position_monitor,
            trigger,
            id=f"{workspace_id}_position",
            name="Position monitor",
        )

        self._logger.info("Position monitor job added: interval=%d min", offset)

    def _add_weekend_check_job(self) -> None:
        """Add the weekend state transition check job (every minute)."""
        workspace_id = self._config.get("workspace_id", "ws")

        trigger = IntervalTrigger(
            minutes=1, timezone=self._profile._tz,
        )

        self._scheduler.add_job(
            self._execute_weekend_check,
            trigger,
            id=f"{workspace_id}_weekend_check",
            name="Weekend state check",
        )

        self._logger.info("Weekend check job added: interval=1 min")

    def _add_daily_report_job(self) -> None:
        """Add the daily P&L report cron job from market profile config."""
        workspace_id = self._config.get("workspace_id", "ws")

        report_cfg = self._profile.get_daily_report_config()
        if not report_cfg:
            self._logger.info("No daily_report config in profile -- skipping")
            return

        trigger = CronTrigger(
            hour=str(report_cfg.get("hour", 17)),
            minute=str(report_cfg.get("minute", 0)),
            day_of_week=str(report_cfg.get("days", "mon-fri")),
            timezone=ZoneInfo(report_cfg.get("timezone", "US/Eastern")),
        )

        self._scheduler.add_job(
            self._execute_daily_report,
            trigger,
            id=f"{workspace_id}_daily_report",
            name="Daily P&L report",
        )

        self._logger.info(
            "Daily report job added: %02d:%02d %s",
            report_cfg.get("hour", 17),
            report_cfg.get("minute", 0),
            report_cfg.get("days", "mon-fri"),
        )

    def _add_weekly_report_job(self) -> None:
        """Add the weekly performance report cron job (Sundays 6PM ET)."""
        workspace_id = self._config.get("workspace_id", "ws")

        # Use daily report timezone config if available, default US/Eastern
        report_cfg = self._profile.get_daily_report_config()
        tz_name = report_cfg.get("timezone", "US/Eastern") if report_cfg else "US/Eastern"

        trigger = CronTrigger(
            day_of_week="sun",
            hour=18,
            minute=0,
            timezone=tz_name,
        )

        self._scheduler.add_job(
            self._execute_weekly_report,
            trigger,
            id=f"{workspace_id}_weekly_report",
            name="Weekly performance report",
        )
        self._logger.info("Weekly report job added: Sun 6PM %s", tz_name)

    def _add_ghost_snipes_job(self) -> None:
        """Add the nightly ghost-snipes batch (23:30 ET) — records every
        validator-origin SNIPE_TRIGGERED and replays guardian-equivalent
        outcomes for both live-fired and gate-blocked snipes."""
        workspace_id = self._config.get("workspace_id", "ws")
        trigger = CronTrigger(hour=23, minute=30, timezone="US/Eastern")
        self._scheduler.add_job(
            self._execute_ghost_snipes,
            trigger,
            id=f"{workspace_id}_ghost_snipes",
            name="Nightly ghost-snipes recorder + replayer + report",
        )
        self._logger.info("Ghost-snipes nightly job added: 23:30 ET daily")

    async def _execute_ghost_snipes(self) -> None:
        """Run scripts/ghost_snipes.py as a subprocess (record + replay + report)."""
        import subprocess
        try:
            script = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "scripts", "ghost_snipes.py",
            )
            result = subprocess.run(
                ["python3", script, "all"],
                capture_output=True, text=True, timeout=600,
            )
            if result.returncode == 0:
                self._logger.info(
                    "[GHOST_SNIPES] Nightly batch complete:\n%s",
                    (result.stdout or "")[-2000:],
                )
            else:
                self._logger.error(
                    "[GHOST_SNIPES] Nightly batch failed (rc=%d): %s",
                    result.returncode, (result.stderr or "")[-1000:],
                )
        except Exception as e:
            self._logger.error("[GHOST_SNIPES] Nightly job failed: %s", e)

    def _add_weekly_setup_discovery_job(self) -> None:
        """Add the weekly setup-discovery scan (Sat 8 AM ET, market closed).

        Runs scripts wrapping setup_discovery.discover_from_conditions to scan
        backtest_trades for new high-edge S21+ indicator combinations and append
        to custom_setups.json. Wired 2026-05-10 — was never scheduled before.
        See .planning/v1.2-audit/LOOP-BREAK-FINDINGS.md.
        """
        workspace_id = self._config.get("workspace_id", "ws")
        # Sat 8 AM ET — market closed (no trading until Sunday 5 PM), fresh week data
        trigger = CronTrigger(day_of_week="sat", hour=8, minute=0, timezone="US/Eastern")
        self._scheduler.add_job(
            self._execute_weekly_setup_discovery,
            trigger,
            id=f"{workspace_id}_weekly_setup_discovery",
            name="Weekly setup-discovery scan",
        )
        self._logger.info("Weekly setup-discovery job added: Sat 8 AM ET")

    async def _execute_weekly_setup_discovery(self) -> None:
        """Run setup_discovery as a subprocess to populate custom_setups.json."""
        import subprocess
        try:
            src_dir = os.path.dirname(os.path.abspath(__file__))
            result = subprocess.run(
                ["python3", "-m", "setup_discovery"],
                cwd=src_dir,
                capture_output=True, text=True, timeout=1800,  # 30 min cap
            )
            if result.returncode == 0:
                self._logger.info(
                    "[SETUP_DISCOVERY] Weekly scan complete:\n%s",
                    (result.stdout or "")[-2000:],
                )
            else:
                self._logger.error(
                    "[SETUP_DISCOVERY] Weekly scan failed (rc=%d): %s",
                    result.returncode, (result.stderr or "")[-1000:],
                )
        except Exception as e:
            self._logger.error("[SETUP_DISCOVERY] Weekly job failed: %s", e)

    def _add_nightly_digest_job(self) -> None:
        """Add the nightly flight recorder digest job (23:55 ET)."""
        workspace_id = self._config.get("workspace_id", "ws")
        trigger = CronTrigger(hour=23, minute=55, timezone="US/Eastern")
        self._scheduler.add_job(
            self._execute_nightly_digest,
            trigger,
            id=f"{workspace_id}_nightly_digest",
            name="Nightly flight recorder digest",
        )
        self._logger.info("Nightly digest job added: 23:55 ET")

    async def _execute_nightly_digest(self) -> None:
        """Write nightly flight recorder digest to logs/nightly_YYYY-MM-DD.json."""
        import json as _json
        from datetime import datetime, timezone as _tz
        try:
            from flight_recorder import FlightRecorder
            recorder = FlightRecorder()
            digest = recorder.get_nightly_digest(hours_back=24)

            logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
            os.makedirs(logs_dir, exist_ok=True)
            date_str = datetime.now(_tz.utc).strftime("%Y-%m-%d")
            out_path = os.path.join(logs_dir, f"nightly_{date_str}.json")
            with open(out_path, "w") as _f:
                _json.dump(digest, _f, indent=2)

            self._logger.info(
                "[FLIGHT] Nightly digest: %d errors, %d warnings, %d cycles — saved to %s",
                digest.get("error_count", 0),
                digest.get("warning_count", 0),
                digest.get("total_cycles", 0),
                out_path,
            )
        except Exception as e:
            self._logger.error("[FLIGHT] Nightly digest failed: %s", e)

    def _add_nightly_qa_audit_job(self) -> None:
        """Add the nightly QA audit job (22:00 ET weeknights).

        Two-step flow: forex_qa_nightly_wrapper.py runs the deterministic data
        pass (headline/rollups/profit zones/drawdowns/snipe quality/scout health/
        tuning impacts/regressions), then Claude Code CLI invokes the
        trade-audit-repair skill for narrative enrichment (vision review of
        losers, tuning recommendations, ghost replay). Uses the local Claude
        Max plan — full access to databases, charts, vault.
        """
        workspace_id = self._config.get("workspace_id", "ws")
        trigger = CronTrigger(
            hour=22, minute=0,
            day_of_week="mon-fri",
            timezone="US/Eastern",
        )
        self._scheduler.add_job(
            self._execute_nightly_qa_audit,
            trigger,
            id=f"{workspace_id}_nightly_qa_audit",
            name="Nightly QA audit (Claude Code)",
        )
        self._logger.info("Nightly QA audit job added: 22:00 ET Mon-Fri")

    async def _execute_nightly_qa_audit(self) -> None:
        """Run the two-step nightly QA audit.

        Step 1: deterministic data pass via ``scripts/forex_qa_nightly_wrapper.py``
        — writes headline metrics, rollups, profit zones, drawdowns, snipe
        quality by origin, scout learning-loop health, tuning impacts, and
        regressions to Forex Trading Team/Reports/qa_audit_{date}.md,
        dashboard/qa_status.json, and the knowledge vault.

        Step 2: narrative enrichment via the ``trade-audit-repair`` skill
        through Claude Code CLI — appends vision review of losing trades,
        tuning recommendations, and ghost validator replay on gray-zone
        trades to the same report file.

        If step 1 fails, step 2 is skipped.
        """
        # Guard: skip if .skip_nightly_jobs exists (e.g., optimizer baseline running)
        skip_file = os.path.join(os.path.dirname(__file__), ".skip_nightly_jobs")
        if os.path.exists(skip_file):
            logger.info("Skipping nightly QA audit — .skip_nightly_jobs guard file present")
            return

        import subprocess
        import sys as _sys
        from datetime import datetime, timezone as _tz

        date_str = datetime.now(ZoneInfo("US/Eastern")).strftime("%Y-%m-%d")
        self._logger.info("[QA_AUDIT] Starting nightly QA audit for %s", date_str)

        src_dir = os.path.dirname(os.path.abspath(__file__))
        logs_dir = os.path.join(src_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        log_path = os.path.join(logs_dir, f"qa_audit_{date_str}.log")
        report_path = (
            f"Forex Trading Team/Reports/qa_audit_{date_str}.md"
        )

        # ---------------------------------------------------------------
        # Step 1: deterministic wrapper
        # ---------------------------------------------------------------
        self._logger.info(
            "[QA_AUDIT] Step 1: deterministic wrapper (forex_qa_nightly_wrapper.py)"
        )
        try:
            step1 = await asyncio.to_thread(
                subprocess.run,
                [
                    _sys.executable,
                    "scripts/forex_qa_nightly_wrapper.py",
                ],
                capture_output=True,
                text=True,
                timeout=600,  # 10 min max
                cwd=src_dir,
            )

            with open(log_path, "w") as f:
                f.write(f"=== QA Audit {date_str} — Step 1 (wrapper) ===\n")
                f.write(f"Exit code: {step1.returncode}\n\n")
                f.write("=== STDOUT ===\n")
                f.write(step1.stdout or "(empty)")
                f.write("\n\n=== STDERR ===\n")
                f.write(step1.stderr or "(empty)")
                f.write("\n\n")

            if step1.returncode != 0:
                self._logger.error(
                    "[QA_AUDIT] Step 1 failed with code %d — log: %s (skipping step 2)",
                    step1.returncode, log_path,
                )
                return

            self._logger.info(
                "[QA_AUDIT] Step 1 complete, written to %s", report_path,
            )

        except subprocess.TimeoutExpired:
            self._logger.error(
                "[QA_AUDIT] Step 1 timed out after 10 min for %s (skipping step 2)",
                date_str,
            )
            return
        except Exception as e:
            self._logger.error(
                "[QA_AUDIT] Step 1 failed: %s (skipping step 2)", e,
            )
            return

        # ---------------------------------------------------------------
        # Step 1.5: Ghost validator batch replay (35B vs Opus comparison)
        # Stops 9B, loads 35B, replays today's validator calls, compares
        # verdicts and outcomes, logs to ghost_verdicts, restarts 9B.
        # Only runs if ghost.enabled and ghost.mode == "batch".
        # ---------------------------------------------------------------
        try:
            from tuning_config import get as _tc_get
            if _tc_get("ghost.enabled", False) and _tc_get("ghost.mode", "batch") == "batch":
                self._logger.info("[QA_AUDIT] Step 1.5: ghost batch replay (35B vs Opus)")
                try:
                    step15 = await asyncio.to_thread(
                        subprocess.run,
                        [
                            _sys.executable, "-m", "optimizer.ghost_replay",
                            "--days", "1", "--force", "--no-teaching",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=2400,  # 40 min max (28 entries × ~45s + model load)
                        cwd=src_dir,
                    )
                    with open(log_path, "a") as f:
                        f.write(f"\n=== QA Audit {date_str} — Step 1.5 (ghost replay) ===\n")
                        f.write(f"Exit code: {step15.returncode}\n\n")
                        f.write(step15.stdout or "(empty)")
                        if step15.stderr:
                            f.write(f"\n\nSTDERR:\n{step15.stderr[:2000]}")
                    if step15.returncode == 0:
                        self._logger.info("[QA_AUDIT] Ghost replay complete")
                    else:
                        self._logger.warning("[QA_AUDIT] Ghost replay exited %d — continuing", step15.returncode)
                except subprocess.TimeoutExpired:
                    self._logger.warning("[QA_AUDIT] Ghost replay timed out — continuing")
                except Exception as e:
                    self._logger.warning("[QA_AUDIT] Ghost replay error: %s — continuing", e)
        except Exception:
            pass  # tuning_config import fail — skip ghost silently

        # ---------------------------------------------------------------
        # Step 2: narrative enrichment via trade-audit-repair skill
        # ---------------------------------------------------------------
        self._logger.info(
            "[QA_AUDIT] Step 2: narrative via Claude Code (trade-audit-repair)"
        )

        prompt = (
            "Run /trade-audit-repair in nightly mode. Today is {date}. "
            "The deterministic report has been written to "
            "'Forex Trading Team/Reports/qa_audit_{date}.md' with headline metrics, "
            "rollups, profit zones, drawdowns, snipe quality by origin, scout learning-loop "
            "health, tuning impacts, and regressions. "
            "Append narrative sections to that file: "
            "(1) vision review of losing trades (generate charts via chart_generator, "
            "run vision analysis per references/vision-audit.md), "
            "(2) specific tuning recommendations with evidence from the report data, "
            "(3) ghost validator replay on gray-zone trades per references/ghost-replay.md. "
            "Write a summary to the vault."
        ).format(date=date_str)

        try:
            step2 = await asyncio.to_thread(
                subprocess.run,
                [
                    "~/.claude/local/claude",
                    "-p", prompt,
                    "--allowedTools", "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch",
                    "--model", "claude-sonnet-4-6",
                    "--max-turns", "50",
                ],
                capture_output=True,
                text=True,
                timeout=1800,  # 30 min max
                cwd="~/Jarvis",
            )

            with open(log_path, "a") as f:
                f.write(f"=== QA Audit {date_str} — Step 2 (narrative) ===\n")
                f.write(f"Exit code: {step2.returncode}\n\n")
                f.write("=== STDOUT ===\n")
                f.write(step2.stdout or "(empty)")
                f.write("\n\n=== STDERR ===\n")
                f.write(step2.stderr or "(empty)")

            if step2.returncode == 0:
                self._logger.info(
                    "[QA_AUDIT] Step 2 complete — log: %s", log_path,
                )
                self._logger.info(
                    "[QA_AUDIT] Nightly audit complete for %s", date_str,
                )
            else:
                self._logger.error(
                    "[QA_AUDIT] Step 2 exited with code %d — log: %s",
                    step2.returncode, log_path,
                )

        except subprocess.TimeoutExpired:
            self._logger.error(
                "[QA_AUDIT] Step 2 timed out after 30 min for %s", date_str,
            )
        except Exception as e:
            self._logger.error("[QA_AUDIT] Step 2 failed: %s", e)

    def _add_nightly_optimizer_job(self) -> None:
        """Add the nightly parameter optimizer job (22:30 ET weeknights).

        Runs the Bayesian optimizer via ``python -m optimizer.results`` and writes
        a markdown report to Forex Trading Team/Reports/optimizer_report_{date}.md.
        Fires 30 minutes after the QA audit (22:00 ET) so reports are ready before
        the end-of-day review.
        """
        workspace_id = self._config.get("workspace_id", "ws")
        trigger = CronTrigger(
            hour=22, minute=30,
            day_of_week="mon-fri",
            timezone="US/Eastern",
        )
        self._scheduler.add_job(
            self._execute_nightly_optimizer,
            trigger,
            id=f"{workspace_id}_nightly_optimizer",
            name="Nightly parameter optimizer",
        )
        self._logger.info("Nightly optimizer job added: 22:30 ET Mon-Fri")

    async def _execute_nightly_optimizer(self) -> None:
        """Run the parameter optimizer as a subprocess.

        Invokes ``python -m optimizer.results --n-calls 500 --tier 1`` with a
        30-minute timeout.  Output is logged to Source/logs/optimizer_{date}.log.
        On success the report appears in Forex Trading Team/Reports/ and any
        proposals surface in the dashboard QA Audit panel.
        """
        # Guard: skip if .skip_nightly_jobs exists (e.g., optimizer baseline running)
        skip_file = os.path.join(os.path.dirname(__file__), ".skip_nightly_jobs")
        if os.path.exists(skip_file):
            logger.info("Skipping nightly optimizer — .skip_nightly_jobs guard file present")
            return

        import subprocess
        import sys as _sys
        from datetime import datetime, timezone as _tz

        date_str = datetime.now(ZoneInfo("US/Eastern")).strftime("%Y-%m-%d")
        self._logger.info("[OPTIMIZER] Starting nightly optimizer for %s", date_str)

        logs_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "logs"
        )
        os.makedirs(logs_dir, exist_ok=True)
        log_path = os.path.join(logs_dir, f"optimizer_{date_str}.log")

        src_dir = os.path.dirname(os.path.abspath(__file__))

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    _sys.executable,
                    "-m", "optimizer.results",
                    "--engine", "v2",
                    "--n-calls", "500",
                    "--no-proposals",
                ],
                capture_output=True,
                text=True,
                timeout=1800,  # 30 min max
                cwd=src_dir,
            )

            with open(log_path, "w") as f:
                f.write(f"=== Optimizer Run {date_str} ===\n")
                f.write(f"Exit code: {result.returncode}\n\n")
                f.write("=== STDOUT ===\n")
                f.write(result.stdout or "(empty)")
                f.write("\n\n=== STDERR ===\n")
                f.write(result.stderr or "(empty)")

            if result.returncode == 0:
                self._logger.info(
                    "[OPTIMIZER] Nightly optimizer completed for %s — log: %s",
                    date_str, log_path,
                )
            else:
                self._logger.error(
                    "[OPTIMIZER] Optimizer exited with code %d — log: %s",
                    result.returncode, log_path,
                )

        except subprocess.TimeoutExpired:
            self._logger.error(
                "[OPTIMIZER] Optimizer timed out after 30 min for %s", date_str,
            )
        except Exception as e:
            self._logger.error("[OPTIMIZER] Optimizer failed: %s", e)

    def _add_tuning_measurement_job(self) -> None:
        """Add the tuning performance measurement job (every 6 hours)."""
        workspace_id = self._config.get("workspace_id", "ws")
        trigger = IntervalTrigger(hours=6, timezone="US/Eastern")
        self._scheduler.add_job(
            self._execute_tuning_measurement,
            trigger,
            id=f"{workspace_id}_tuning_measurement",
            name="Tuning performance measurement",
        )
        self._logger.info("Tuning measurement job added: every 6h")

    async def _execute_tuning_measurement(self) -> None:
        """Measure tuning change impact at 24h/48h/7d/14d windows."""
        try:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from tuning_config import measure_change_impact
            results = await asyncio.to_thread(measure_change_impact)
            if results:
                self._logger.info(
                    "[TUNING_MEASURE] Captured %d snapshots: %s",
                    len(results),
                    ", ".join(f"{r['param']}/{r['window']}={r['verdict']}" for r in results[:5]),
                )
            else:
                self._logger.debug("[TUNING_MEASURE] No new windows to measure")
        except Exception as e:
            self._logger.error("[TUNING_MEASURE] Failed: %s", e)

    def _add_news_monitor_job(self) -> None:
        """Add the news monitoring interval job."""
        workspace_id = self._config.get("workspace_id", "ws")

        # Check for workspace-level override
        news_cfg = self._config.get("news_monitoring", {})
        override = news_cfg.get("override_interval_minutes")
        if override:
            interval = int(override)
        else:
            profile_news = self._profile.get_news_config()
            interval = int(profile_news.get("interval_minutes", 5))

        trigger = IntervalTrigger(
            minutes=interval, timezone=self._profile._tz,
        )

        self._scheduler.add_job(
            self._execute_news_monitor,
            trigger,
            id=f"{workspace_id}_news",
            name="News monitor",
        )

        self._logger.info("News monitor job added: interval=%d min", interval)

    # ------------------------------------------------------------------
    # SwarmHandler lazy singleton
    # ------------------------------------------------------------------

    def _get_swarm(self):
        """Lazy-load SwarmHandler for swarm-driven execution."""
        if not hasattr(self, '_swarm') or self._swarm is None:
            try:
                from Handler.handler_swarm import SwarmHandler
                self._swarm = SwarmHandler()
            except ImportError:
                self._logger.warning("SwarmHandler not available")
                self._swarm = None
        return self._swarm

    async def _swarm_distribute(self, tasks: list, strategy: str = "round_robin") -> dict:
        """Execute SwarmHandler.distribute_tasks asynchronously."""
        swarm = self._get_swarm()
        if swarm is None:
            return {}
        result = await swarm.handle({
            "action": "distribute_tasks",
            "parameters": {"tasks": tasks, "strategy": strategy},
        })
        return result.data if hasattr(result, "data") else {}

    async def _swarm_execute_tool(self, agent_name: str, tool_name: str, **kwargs) -> dict:
        """Execute SwarmHandler.execute_tool asynchronously."""
        swarm = self._get_swarm()
        if swarm is None:
            return {}
        result = await swarm.handle({
            "action": "execute_tool",
            "parameters": {"agent_name": agent_name, "tool_name": tool_name, **kwargs},
        })
        return result.data if hasattr(result, "data") else {}

    # ------------------------------------------------------------------
    # Job execution (private) -- ALL market-hours-guarded
    # ------------------------------------------------------------------

    async def _execute_cycle(self) -> None:
        """Execute one trading cycle for all configured instruments.

        Drives agent execution through SwarmHandler.distribute_tasks()
        to the cycle_orchestrator agent.
        Guarded by market hours and trading pause state.
        """
        # Guard 1: market hours
        if not self._profile.is_market_open():
            self._logger.debug("Cycle skipped: market closed")
            return

        # Guard 2: trading pause (module-level flag in trading_cycle)
        from Source.agents.trading_cycle import _trading_paused
        if _trading_paused:
            self._logger.info("Cycle skipped: trading paused")
            return

        # Lazy import TradingCycle
        if self._cycle is None:
            from Source.agents.trading_cycle import TradingCycle
            from Source.agents.team_setup import TradingTeamSetup
            from Source.agents.comment_protocol import CommentProtocol
            from Source.journey_tracker import JourneyTracker

            # Create lightweight tracker for fast startup
            tracker = JourneyTracker(str(Path(__file__).parent.parent.parent / "Database" / "v2" / "journeys.db"))
            team = TradingTeamSetup(tracker=tracker)
            protocol = CommentProtocol()
            self._cycle = TradingCycle(team, protocol)

        start = _time.monotonic()

        # Distribute cycle tasks to cycle_orchestrator via SwarmHandler
        cycle_tasks = [
            {
                "id": f"cycle_{inst}_{self._primary_timeframe}",
                "description": f"Trading cycle for {inst} ({self._primary_timeframe})",
                "tool": "run_cycle",
                "args": {"instrument": inst, "timeframe": self._primary_timeframe},
            }
            for inst in self._instruments
        ]
        await self._swarm_distribute(cycle_tasks, strategy="round_robin")

        for instrument in self._instruments:
            try:
                # TradingCycle.run_cycle internally uses SwarmHandler for all agent calls
                result = await asyncio.to_thread(
                    self._cycle.run_cycle,
                    instrument,
                    self._primary_timeframe,
                )
                action = result.get("decision", {}).get("action", "hold")
                error = result.get("error")
                if error:
                    self._logger.warning(
                        "Cycle %s: error=%s", instrument, error,
                    )
                else:
                    self._logger.info(
                        "Cycle %s: action=%s", instrument, action,
                    )
            except Exception as exc:
                self._logger.error(
                    "Cycle %s failed: %s", instrument, exc,
                )
                # Log error for external bot querying (LOGS-07)
                try:
                    from Source.trade_logger import TradeLogger

                    tl = TradeLogger()
                    tl.log_mcp_query(
                        cycle_id=f"error_{int(_time.time())}",
                        instrument=instrument,
                        source="cycle_error",
                        query_type="error",
                        response_summary={"error": str(exc)},
                        impact_on_decision="Cycle aborted",
                        error=str(exc),
                    )
                    tl.close()
                except Exception:
                    pass

        elapsed = _time.monotonic() - start
        self._logger.info("Cycle complete: %.1fs", elapsed)

    async def _execute_position_monitor(self) -> None:
        """Run position monitoring for all configured instruments.

        Uses SwarmHandler for agent execution.
        Guarded by market hours and trading pause state.
        """
        if not self._profile.is_market_open():
            self._logger.debug("Position monitor skipped: market closed")
            return

        from Source.agents.trading_cycle import _trading_paused
        if _trading_paused:
            self._logger.info("Position monitor skipped: trading paused")
            return

        # Lazy import TradingCycle
        if self._cycle is None:
            from Source.agents.trading_cycle import TradingCycle
            from Source.agents.team_setup import TradingTeamSetup
            from Source.agents.comment_protocol import CommentProtocol
            from Source.journey_tracker import JourneyTracker

            # Create lightweight tracker for fast startup
            tracker = JourneyTracker(str(Path(__file__).parent.parent.parent / "Database" / "v2" / "journeys.db"))
            team = TradingTeamSetup(tracker=tracker)
            protocol = CommentProtocol()
            self._cycle = TradingCycle(team, protocol)

        try:
            # Distribute position monitoring via swarm
            await self._swarm_distribute([{
                "id": "position_monitor",
                "description": f"Position monitor for {', '.join(self._instruments)}",
                "tool": "run_position_update",
                "args": {"instruments": self._instruments},
            }])

            # TradingCycle.run_position_update internally uses SwarmHandler
            result = await asyncio.to_thread(
                self._cycle.run_position_update,
                self._instruments,
            )
            actions = result.get("actions_taken", [])
            self._logger.info(
                "Position monitor: checked=%d actions=%d",
                result.get("instruments_checked", 0),
                len(actions),
            )
        except Exception as exc:
            self._logger.error("Position monitor failed: %s", exc)

    async def _execute_weekend_check(self) -> None:
        """Check for weekend state transitions (every minute)."""
        if self._weekend_manager:
            self._weekend_manager.check_transition()

    async def _execute_daily_report(self) -> None:
        """Generate and log the daily P&L report via SwarmHandler.

        Routes through SwarmHandler.execute_tool to the reporter agent.
        """
        try:
            result = await self._swarm_execute_tool(
                "reporter", "generate_daily_report",
                instruments=self._instruments,
            )
            tool_result = result.get("tool_result", result)
            formatted = tool_result.get("formatted_text", "") if isinstance(tool_result, dict) else ""
            self._logger.info(
                "Daily report generated:\n%s", formatted,
            )
        except Exception as exc:
            self._logger.error("Daily report failed: %s", exc)

    async def _execute_weekly_report(self) -> None:
        """Generate and log the weekly performance report via SwarmHandler.

        Routes through SwarmHandler.execute_tool to the reporter agent.
        """
        try:
            result = await self._swarm_execute_tool(
                "reporter", "generate_weekly_report",
                instruments=self._instruments,
            )
            tool_result = result.get("tool_result", result)
            formatted = tool_result.get("formatted_text", "") if isinstance(tool_result, dict) else ""
            self._logger.info("Weekly report generated:\n%s", formatted)
        except Exception as exc:
            self._logger.error("Weekly report failed: %s", exc)

    async def _execute_news_monitor(self) -> None:
        """Run news monitoring for all configured instruments via SwarmHandler.

        Distributes intelligence gathering across wolfram/news/weather
        agents via SwarmHandler.distribute_tasks.
        Guarded by market hours (NOT _trading_paused -- intelligence is passive).
        """
        if not self._profile.is_market_open():
            self._logger.debug("News monitor skipped: market closed")
            return

        for instrument in self._instruments:
            try:
                # Distribute intelligence tasks to 3 agents
                intel_tasks = [
                    {"id": f"news_{instrument}", "description": f"News for {instrument}",
                     "tool": "get_news", "args": {"instrument": instrument}},
                    {"id": f"weather_{instrument}", "description": f"Weather for {instrument}",
                     "tool": "get_weather", "args": {"instrument": instrument}},
                    {"id": f"wolfram_{instrument}", "description": f"Wolfram for {instrument}",
                     "tool": "query", "args": {"instrument": instrument}},
                ]
                await self._swarm_distribute(intel_tasks, strategy="round_robin")

                # Execute news check via swarm
                news_result = await self._swarm_execute_tool(
                    "news_analyst", "get_news",
                    instrument=instrument,
                )
                result = news_result.get("tool_result", news_result)

                # Log high-impact events
                if isinstance(result, dict):
                    high_impact = result.get("high_impact_events", [])
                    if high_impact:
                        self._logger.warning(
                            "HIGH-IMPACT events for %s: %d events",
                            instrument, len(high_impact),
                        )
                    if not result.get("trading_allowed", True):
                        self._logger.warning(
                            "Intelligence halts trading for %s", instrument,
                        )
            except Exception as exc:
                self._logger.error(
                    "News monitor %s failed: %s", instrument, exc,
                )

    # ------------------------------------------------------------------
    # Weekend state
    # ------------------------------------------------------------------

    def get_weekend_state(self) -> str:
        """Return the current weekend manager state.

        Returns:
            State string ('TRADING', 'WEEKEND', etc.) or 'N/A' for
            continuous markets that have no weekend management.
        """
        if self._weekend_manager is None:
            return "N/A"
        return self._weekend_manager.get_state()

    # ------------------------------------------------------------------
    # Configuration update (for orchestrator agent)
    # ------------------------------------------------------------------

    def update_config(self, updates: dict) -> None:
        """Deep-merge updates into the workspace config.

        If market_profile or primary_timeframe changed, reloads the
        schedule from the new profile.  If the scheduler is running,
        stops and restarts with the new configuration.

        Args:
            updates: Dict of fields to merge into the config.
        """
        old_profile = self._config.get("market_profile")
        old_tf = self._config.get("primary_timeframe")

        self._deep_merge(self._config, updates)

        new_profile = self._config.get("market_profile")
        new_tf = self._config.get("primary_timeframe")

        # Reload profile and schedule if changed
        if new_profile != old_profile:
            self._profile = MarketProfile.from_market_type(new_profile)

        if new_profile != old_profile or new_tf != old_tf:
            self._primary_timeframe = new_tf
            self._schedule = self._profile.get_schedule_for_timeframe(new_tf)
            overrides = self._config.get("schedule_override")
            if overrides and isinstance(overrides, dict):
                self._schedule.update(overrides)

        # Update instruments
        self._instruments = list(self._config.get("instruments", self._instruments))

        # Restart scheduler if running
        if self._running:
            self.stop()
            self._cycle = None  # Reset lazy singleton
            self.start()

        self._logger.info("Config updated")

    def save_config(self, path: Optional[str] = None) -> None:
        """Write current workspace config to YAML.

        Args:
            path: Target file path.  If None, raises ValueError.
        """
        if not path:
            raise ValueError("Provide a path to save the workspace config")

        with open(path, "w") as f:
            yaml.dump(
                self._config, f, default_flow_style=False, sort_keys=False,
            )
        self._logger.info("Config saved to %s", path)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _deep_merge(base: dict, updates: dict) -> None:
        """Recursively merge updates into base dict (in-place)."""
        for key, value in updates.items():
            if (
                key in base
                and isinstance(base[key], dict)
                and isinstance(value, dict)
            ):
                WorkspaceScheduleManager._deep_merge(base[key], value)
            else:
                base[key] = value


class SchedulerOrchestrator:
    """Manages multiple WorkspaceScheduleManagers.

    Scans a directory of workspace schedule YAML files, creates a
    manager for each, and provides start/stop/status for all.

    Args:
        schedules_dir: Directory containing workspace schedule YAML files.
            Defaults to ``Config/workspace_schedules/``.
    """

    def __init__(self, schedules_dir: Optional[str] = None) -> None:
        self._schedules_dir = schedules_dir or _DEFAULT_SCHEDULES_DIR
        self._managers: Dict[str, WorkspaceScheduleManager] = {}
        self._logger = logging.getLogger("trading.scheduler.orchestrator")

    def load_all(self) -> int:
        """Scan the schedules directory and create managers for each YAML file.

        Returns:
            Number of workspace configs loaded.
        """
        schedules_path = Path(self._schedules_dir)
        if not schedules_path.exists():
            self._logger.warning(
                "Schedules directory not found: %s", self._schedules_dir,
            )
            return 0

        loaded = 0
        for yaml_file in sorted(schedules_path.glob("*.yaml")):
            try:
                mgr = WorkspaceScheduleManager(config_path=str(yaml_file))
                ws_id = mgr._config.get("workspace_id", yaml_file.stem)
                self._managers[ws_id] = mgr
                loaded += 1
                self._logger.info(
                    "Loaded workspace: %s (%s)",
                    ws_id, mgr._config.get("team_name", "?"),
                )
            except Exception as exc:
                self._logger.error(
                    "Failed to load %s: %s", yaml_file.name, exc,
                )

        self._logger.info("Loaded %d workspace schedule(s)", loaded)
        return loaded

    def start_all(self) -> int:
        """Start all managers that have auto_start=True.

        Returns:
            Number of workspaces started.
        """
        started = 0
        for ws_id, mgr in self._managers.items():
            if mgr._config.get("auto_start", False):
                try:
                    mgr.start()
                    started += 1
                except Exception as exc:
                    self._logger.error(
                        "Failed to start %s: %s", ws_id, exc,
                    )

        self._logger.info("Started %d workspace(s)", started)
        return started

    def stop_all(self) -> None:
        """Stop all running managers."""
        for ws_id, mgr in self._managers.items():
            if mgr.is_running:
                try:
                    mgr.stop()
                except Exception as exc:
                    self._logger.error(
                        "Failed to stop %s: %s", ws_id, exc,
                    )
        self._logger.info("All workspaces stopped")

    def add_workspace(
        self,
        config_path: Optional[str] = None,
        config_dict: Optional[dict] = None,
    ) -> WorkspaceScheduleManager:
        """Add a workspace to the orchestrator.

        Args:
            config_path: Path to workspace schedule YAML.
            config_dict: Workspace config as a dict.

        Returns:
            The created WorkspaceScheduleManager (caller can start it).
        """
        mgr = WorkspaceScheduleManager(
            config_path=config_path, config_dict=config_dict,
        )
        ws_id = mgr._config.get("workspace_id", "unknown")
        self._managers[ws_id] = mgr
        self._logger.info("Added workspace: %s", ws_id)
        return mgr

    def remove_workspace(self, workspace_id: str) -> None:
        """Stop and remove a workspace manager.

        Args:
            workspace_id: ID of the workspace to remove.
        """
        mgr = self._managers.pop(workspace_id, None)
        if mgr:
            if mgr.is_running:
                mgr.stop()
            self._logger.info("Removed workspace: %s", workspace_id)
        else:
            self._logger.warning("Workspace not found: %s", workspace_id)

    def get_workspace(self, workspace_id: str) -> Optional[WorkspaceScheduleManager]:
        """Return a workspace manager by ID.

        Args:
            workspace_id: ID to look up.

        Returns:
            WorkspaceScheduleManager if found, else None.
        """
        return self._managers.get(workspace_id)

    def get_all_status(self) -> List[dict]:
        """Return status from all workspace managers.

        Returns:
            List of status dicts from each manager.
        """
        return [mgr.get_status() for mgr in self._managers.values()]

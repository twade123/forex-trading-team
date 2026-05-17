#!/usr/bin/env python3
"""
Trade Scout - Live market scanning for elite trading setups (Fixed Import Version)

Continuously monitors all 13 forex pairs for high-probability trade setups
based on the elite playbook (win_rate >= 88%, trade_count >= 1000, profit_factor > 1.2).

Features:
- Async parallel scanning across all pairs
- Real-time indicator calculations
- WebSocket alerts to dashboard
- SQLite alert history
- Elite playbook filtering
- 5-minute scan intervals
"""

# Set trading mode for faster database discovery
import os
os.environ['JARVIS_TRADING_MODE'] = '1'

import asyncio
import json
import sqlite3
import time
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional
import pandas as pd

# Add current directory to Python path for absolute imports
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

# ── FUSE cleanup: purge stale -shm and .fuse_hidden files BEFORE any DB access
try:
    from fuse_cleanup import cleanup_fuse_artifacts
    cleanup_fuse_artifacts()
except Exception:
    pass  # non-fatal — db_pool has its own per-DB cleanup as fallback

# Import connection pool and canonical database path
from db_pool import get_trading_forex
from db_connection import get_db, DB_PATH

# Import existing modules with absolute imports
# Import oanda_client with absolute import fallback
try:
    from Source.oanda_client import OandaClient
except ImportError:
    from oanda_client import OandaClient
from backtester.sniper_v4 import add_enhanced_indicators, score_v4, TF_PARAMS
from backtester.indicators import (
    sma, ema, rsi, stochastic, bollinger_bands, macd, parabolic_sar, atr, adx
)
from backtester.candle_patterns import detect_all_patterns
from backtester.ema_separation import scan_ema_signals, generate_market_picture
from market_sessions import get_session_quality, is_prime_time, get_active_sessions
from flight_recorder import flight, FlightStage
from thesis_measurements import compute_thesis_measurements

# Tuning config — central parameter store
try:
    from tuning_config import get as tc_get
except ImportError:
    tc_get = lambda param, fallback=None: fallback

# Import the new Scout Profile Engine
try:
    from Source.scout_profiles import ScoutProfileEngine
except ImportError:
    from scout_profiles import ScoutProfileEngine

# WebSocket for alerts (install with: pip install websockets)
try:
    import websockets
except ImportError:
    logging.getLogger(__name__).warning(
        "websockets not installed — real-time alerts disabled. Install with: pip install websockets"
    )
    websockets = None

logger = logging.getLogger(__name__)


def _compute_rsi_series(closes, period=14):
    """Compute RSI for a list of close prices."""
    rsi = [50.0] * len(closes)
    if len(closes) < period + 1:
        return rsi
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(0, d))
        losses.append(max(0, -d))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        rsi[i + 1] = 100 - (100 / (1 + ag / al)) if al != 0 else 100.0
    return rsi


def _compute_macd_hist_series(closes, fast=12, slow=26, signal=9):
    """Compute MACD histogram for a list of close prices."""
    def _ema(data, period):
        result = [data[0]] if data else []
        mult = 2 / (period + 1)
        for i in range(1, len(data)):
            result.append(data[i] * mult + result[-1] * (1 - mult))
        return result
    if len(closes) < slow + signal:
        return [0.0] * len(closes)
    ef = _ema(closes, fast)
    es = _ema(closes, slow)
    ml = [f - s for f, s in zip(ef, es)]
    sl = _ema(ml, signal)
    return [m - s for m, s in zip(ml, sl)]


def _detect_divergence_at_current(closes, rsi_vals, macd_vals, lookback=20, order=5):
    """Detect all 6 divergence types at the current (last) bar."""
    result = {
        'rsi_bull_div': False, 'rsi_bear_div': False,
        'rsi_hidden_bull_div': False, 'rsi_hidden_bear_div': False,
        'macd_bull_div': False, 'macd_bear_div': False,
        'divergence_types': []
    }
    n = len(closes)
    if n < 2 * order + 2:
        return result
    
    # Find swing points
    highs, lows = [], []
    for i in range(order, n - order):
        if all(closes[i] >= closes[i-j] for j in range(1, order+1)) and \
           all(closes[i] >= closes[i+j] for j in range(1, order+1)):
            highs.append(i)
        if all(closes[i] <= closes[i-j] for j in range(1, order+1)) and \
           all(closes[i] <= closes[i+j] for j in range(1, order+1)):
            lows.append(i)
    
    bar_idx = n - 1
    PROPAGATE = 3  # divergence relevant within 3 bars of swing
    
    # RSI Regular Bullish: price LL, RSI HL
    for i in range(1, len(lows)):
        prev, curr = lows[i-1], lows[i]
        if curr - prev > lookback: continue
        if closes[curr] < closes[prev] and rsi_vals[curr] > rsi_vals[prev]:
            if bar_idx - curr <= PROPAGATE:
                result['rsi_bull_div'] = True
                result['divergence_types'].append('RSI_BULL')
    
    # RSI Regular Bearish: price HH, RSI LH
    for i in range(1, len(highs)):
        prev, curr = highs[i-1], highs[i]
        if curr - prev > lookback: continue
        if closes[curr] > closes[prev] and rsi_vals[curr] < rsi_vals[prev]:
            if bar_idx - curr <= PROPAGATE:
                result['rsi_bear_div'] = True
                result['divergence_types'].append('RSI_BEAR')
    
    # RSI Hidden Bullish: price HL, RSI LL (continuation UP)
    for i in range(1, len(lows)):
        prev, curr = lows[i-1], lows[i]
        if curr - prev > lookback: continue
        if closes[curr] > closes[prev] and rsi_vals[curr] < rsi_vals[prev]:
            if bar_idx - curr <= PROPAGATE:
                result['rsi_hidden_bull_div'] = True
                result['divergence_types'].append('RSI_HIDDEN_BULL')
    
    # RSI Hidden Bearish: price LH, RSI HH (continuation DOWN)
    for i in range(1, len(highs)):
        prev, curr = highs[i-1], highs[i]
        if curr - prev > lookback: continue
        if closes[curr] < closes[prev] and rsi_vals[curr] > rsi_vals[prev]:
            if bar_idx - curr <= PROPAGATE:
                result['rsi_hidden_bear_div'] = True
                result['divergence_types'].append('RSI_HIDDEN_BEAR')
    
    # MACD Bullish: price LL, MACD histogram HL
    for i in range(1, len(lows)):
        prev, curr = lows[i-1], lows[i]
        if curr - prev > lookback: continue
        if closes[curr] < closes[prev] and macd_vals[curr] > macd_vals[prev]:
            if bar_idx - curr <= PROPAGATE:
                result['macd_bull_div'] = True
                result['divergence_types'].append('MACD_BULL')
    
    # MACD Bearish: price HH, MACD histogram LH
    for i in range(1, len(highs)):
        prev, curr = highs[i-1], highs[i]
        if curr - prev > lookback: continue
        if closes[curr] > closes[prev] and macd_vals[curr] < macd_vals[prev]:
            if bar_idx - curr <= PROPAGATE:
                result['macd_bear_div'] = True
                result['divergence_types'].append('MACD_BEAR')
    
    return result


class TradeScout:
    def __init__(self, api_key_file: str = None,
                 account_id: str = None,
                 practice_url: str = "https://api-fxpractice.oanda.com",
                 db_path: str = None,
                 user_id: int = None):

        import config as _cfg
        self.api_key_file = api_key_file  # legacy — prefer config.API_KEY
        self._api_key = _cfg.API_KEY  # sourced from config (DB → env → file)
        self.account_id = account_id or _cfg.ACCOUNT_ID
        self.practice_url = practice_url
        self.db_path = db_path or DB_PATH
        self._user_id = user_id

        # Trading pairs to monitor
        self.pairs = [
            "AUD_JPY", "AUD_USD", "EUR_AUD", "EUR_CHF", "EUR_GBP",
            "EUR_JPY", "EUR_USD", "GBP_JPY", "GBP_USD", "NZD_USD",
            "USD_CAD", "USD_CHF", "USD_JPY"
        ]

        self.timeframe = "M15"  # Primary scanning timeframe for setups (FIXED: was H1)
        self.ema_timeframe = "M15"  # EMA separation computed on M15 for faster signals
        self.velocity_timeframe = "M15"  # M15 for faster velocity tracking
        self.scan_interval = 900  # 15 minutes - aligned to M15 candle boundaries
        self.running = False

        # Elite playbook cache
        self.elite_playbook = []
        self.scout_thresholds = {}

        # WebSocket connections for dashboard alerts
        self.websocket_clients = set()

        # Market picture cache (latest per pair)
        self._latest_market_pictures = {}

        # Cooldown: skip pair for 1 candle after HOLD/REJECT cycle
        # Key: pair → Unix timestamp when cooldown expires
        # Persisted to disk so restarts don't wipe active cooldowns.
        self._COOLDOWN_FILE = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', 'dashboard', 'pair_cooldowns.json'
        )
        self._pair_cooldowns: Dict[str, float] = self._load_cooldowns()
        self._COOLDOWN_SCANS = 1  # Skip 1 scan after rejection (= 1 M15 candle)

        # Scout Profile Engine for advanced pattern matching
        logger.info("Initializing Scout Profile Engine (this takes ~47 seconds)...")
        start_time = time.time()
        try:
            self.profile_engine = ScoutProfileEngine(self.db_path)
            init_time = time.time() - start_time
            logger.info(f"Scout Profile Engine initialized in {init_time:.2f}s with {len(self.profile_engine.profiles):,} profiles")
        except Exception as e:
            logger.error(f"Failed to initialize Scout Profile Engine: {e}")
            self.profile_engine = None

        # Add scan cycle counter for periodic tasks
        self.scan_cycle_count = 0

        self._setup_database()
        self._load_playbook_setups()
        self._load_scout_thresholds()
        self._load_combined_playbook()

    def _load_cooldowns(self) -> Dict[str, float]:
        """Load persisted pair cooldowns from disk. Prunes expired entries.
        Also restores cooldowns from flight recorder for any pairs not in the file
        (handles case where cooldown file was missing but cycles ran recently).
        """
        now = time.time()
        active = {}

        # 1. Load from persisted file
        try:
            _path = os.path.normpath(self._COOLDOWN_FILE)
            if os.path.exists(_path):
                with open(_path) as f:
                    data = json.load(f)
                active = {k: v for k, v in data.items() if v > now}
                logger.info("Cooldowns from file: %d active", len(active))
        except Exception as _e:
            logger.debug("Could not load pair cooldowns file: %s", _e)

        # 2. Restore from flight recorder for pairs not already covered
        _fr_conn = None
        try:
            from datetime import datetime as _dtt, timezone as _tz2
            _fr_db = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), 'flight_recorder.db'))
            _fr_conn = __import__('sqlite3').connect(_fr_db, timeout=3, isolation_level=None)
            rows = _fr_conn.execute("""
                SELECT pair, MAX(timestamp) as last_cycle
                FROM flight_log
                WHERE stage = 'cycle_end'
                  AND timestamp >= datetime('now', '-15 minutes')
                GROUP BY pair
            """).fetchall()
            for _pair, _ts_str in rows:
                if _pair in active:
                    continue  # already covered by file
                try:
                    # Parse ISO timestamp — handle both +00:00 and no-tz formats
                    _ts_str = _ts_str.rstrip('Z')
                    if '+' not in _ts_str and 'T' in _ts_str:
                        _ts_str += '+00:00'
                    _last = _dtt.fromisoformat(_ts_str)
                    if _last.tzinfo is None:
                        _last = _last.replace(tzinfo=_tz2.utc)
                    _elapsed = (_dtt.now(_tz2.utc) - _last).total_seconds()
                    _remain = 900 - _elapsed
                    if _remain > 0:
                        active[_pair] = now + _remain
                        logger.info("Cooldown restored from flight log: %s — %ds remaining", _pair, int(_remain))
                except Exception:
                    pass
        except Exception as _fr_e:
            logger.debug("Flight recorder cooldown restore failed: %s", _fr_e)
        finally:
            if _fr_conn is not None:
                try:
                    _fr_conn.close()
                except Exception:
                    pass

        return active

    def _save_cooldowns(self):
        """Persist current pair cooldowns to disk."""
        try:
            _path = os.path.normpath(self._COOLDOWN_FILE)
            os.makedirs(os.path.dirname(_path), exist_ok=True)
            now = time.time()
            active = {k: v for k, v in self._pair_cooldowns.items() if v > now}
            with open(_path, 'w') as f:
                json.dump(active, f)
        except Exception as _e:
            logger.debug("Could not save pair cooldowns: %s", _e)

    def _load_combined_playbook(self):
        """Load combined thesis+snipe playbook (37 strategies across 12 thesis setups)."""
        self._thesis_elite = {}       # key: (pair, direction) → list of width ranges (for PATH B gate)
        self._combined_playbook = []   # full playbook entries
        self._snipe_context_index = {} # key: (pair, snipe_direction) → list of playbook entries
        
        config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Config")
        
        # Load combined playbook
        combined_path = os.path.join(config_dir, "combined_playbook.json")
        try:
            with open(combined_path) as f:
                self._combined_playbook = json.load(f)
            
            # Build thesis elite index (for PATH B gating)
            for e in self._combined_playbook:
                if e.get('play_type') == 'thesis_only':
                    key = (e['pair'], e['direction'])
                    if key not in self._thesis_elite:
                        self._thesis_elite[key] = []
                    self._thesis_elite[key].append({
                        'fan_width_min': e.get('fan_width_min', 0.0),
                        'fan_width_max': e.get('fan_width_max', 999.0),
                        'win_rate': e['thesis_wr'],
                        'profit_factor': e['thesis_pf'],
                        'trades': e['thesis_trades'],
                    })
            
            # Build snipe context index (for PATH A enrichment)
            for e in self._combined_playbook:
                if e.get('snipe_required'):
                    snipe_dir = e.get('snipe_direction', e['direction'])
                    key = (e['pair'], snipe_dir)
                    if key not in self._snipe_context_index:
                        self._snipe_context_index[key] = []
                    self._snipe_context_index[key].append(e)
            
            thesis_count = sum(1 for e in self._combined_playbook if e['play_type'] == 'thesis_only')
            snipe_count = sum(1 for e in self._combined_playbook if e.get('snipe_required'))
            logger.info(
                f"Combined playbook: {len(self._combined_playbook)} entries "
                f"({thesis_count} thesis + {snipe_count} snipe combos) "
                f"across {len(self._thesis_elite)} pair+direction thesis setups"
            )
        except FileNotFoundError:
            logger.warning("No combined playbook found — loading legacy thesis elite")
            # Fallback to old file
            try:
                with open(os.path.join(config_dir, "thesis_elite_playbook.json")) as f:
                    entries = json.load(f)
                for e in entries:
                    key = (e['pair'], e['direction'])
                    if key not in self._thesis_elite:
                        self._thesis_elite[key] = []
                    self._thesis_elite[key].append({
                        'fan_width_min': e.get('fan_width_min', 0.0),
                        'fan_width_max': e.get('fan_width_max', 999.0),
                        'win_rate': e['win_rate'],
                        'profit_factor': e['profit_factor'],
                        'trades': e['trades'],
                    })
                logger.info(f"Legacy playbook: {len(entries)} thesis elite setups")
            except Exception:
                logger.warning("No playbook found at all — all entries allowed")
        except Exception as e:
            logger.error(f"Failed to load combined playbook: {e}")

    def _check_thesis_elite(self, pair: str, direction: str, fan_width_pct: float) -> dict:
        """Check if a thesis entry matches an elite playbook setup (PATH B gate)."""
        key = (pair, direction)
        if key not in self._thesis_elite:
            return {'elite': False, 'reason': f'no elite entry for {pair} {direction}'}
        for setup in self._thesis_elite[key]:
            if setup['fan_width_min'] <= fan_width_pct < setup['fan_width_max']:
                return {
                    'elite': True,
                    'win_rate': setup['win_rate'],
                    'profit_factor': setup['profit_factor'],
                    'trades': setup['trades'],
                    'width_range': f"{setup['fan_width_min']:.2f}-{setup['fan_width_max']:.2f}%",
                }
        return {'elite': False, 'reason': f'fan width {fan_width_pct:.3f}% not in elite ranges'}

    def _check_snipe_thesis_context(self, pair: str, snipe_direction: str, 
                                      fan_state: str, fan_width_pct: float,
                                      thesis_direction: str = None) -> dict:
        """Check if a snipe fires during a thesis window and what play type it matches.
        
        Returns:
            {
                'has_context': True/False,
                'play_type': 'trend_confirmation' | 'continuation' | 'exhaustion_reversal' | ...,
                'play_id': 'T05',
                'snipe_wr': 78.8,
                'snipe_pf': 2.71,
                'boost': +20 (score adjustment),
                'block': False,
                'reason': 'EUR_USD buy early+with = 78.8% WR PF 2.71',
            }
        """
        key = (pair, snipe_direction)
        candidates = self._snipe_context_index.get(key, [])
        if not candidates:
            return {'has_context': False, 'boost': 0, 'block': False}
        
        # Determine thesis phase from fan state
        # early = expanding/accelerating, mid = peaked, late = decelerating/contracting
        phase_map = {
            'expanding': 'early', 'accelerating': 'early',
            'peaked': 'mid', 'stable': 'mid',
            'decelerating': 'late', 'contracting': 'late',
            'crossed': 'early', 'forming': 'early',
        }
        phase = phase_map.get(fan_state, 'mid')
        
        # Determine alignment
        if thesis_direction:
            alignment = 'with' if snipe_direction == thesis_direction else 'against'
        else:
            alignment = None
        
        best_match = None
        best_pf = 0
        
        for entry in candidates:
            # Check fan width range
            if not (entry['fan_width_min'] <= fan_width_pct < entry['fan_width_max']):
                continue
            # Check phase match
            if entry.get('snipe_phase') != phase:
                continue
            # Check alignment match
            if alignment and entry.get('snipe_alignment') != alignment:
                continue
            # Best by PF
            if entry.get('snipe_pf', 0) > best_pf:
                best_pf = entry['snipe_pf']
                best_match = entry
        
        if not best_match:
            # Check if this is a BLOCKED combo (known losing — opposite of elite)
            # If we have entries for this pair but none match → it's uncharted
            return {'has_context': False, 'boost': 0, 'block': False}
        
        # Calculate boost based on play type and strength
        play_type = best_match['play_type']
        snipe_wr = best_match.get('snipe_wr', 50)
        snipe_pf = best_match.get('snipe_pf', 1.0)
        
        # Boost formula: higher WR and PF = more boost
        if snipe_pf >= 3.0:
            boost = 25  # Massive — near-certain setup
        elif snipe_pf >= 2.0:
            boost = 20  # Very strong
        elif snipe_pf >= 1.5:
            boost = 15  # Strong
        elif snipe_pf >= 1.2:
            boost = 10  # Good
        else:
            boost = 5   # Marginal
        
        # Extra boost for very high WR
        if snipe_wr >= 80:
            boost += 10
        elif snipe_wr >= 70:
            boost += 5
        
        return {
            'has_context': True,
            'play_type': play_type,
            'play_id': best_match['id'],
            'thesis_direction': best_match['direction'],
            'snipe_phase': phase,
            'snipe_alignment': best_match.get('snipe_alignment'),
            'snipe_wr': snipe_wr,
            'snipe_pf': snipe_pf,
            'snipe_trades': best_match.get('snipe_trades'),
            'snipe_setups': best_match.get('snipe_setups', []),
            'boost': boost,
            'block': False,
            'reason': (
                f"{pair} {snipe_direction} {phase}+{best_match.get('snipe_alignment', '?')} "
                f"= {snipe_wr}% WR PF {snipe_pf} ({play_type})"
            ),
        }

    def _setup_database(self):
        """Create alerts table if it doesn't exist."""
        conn = get_trading_forex()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scout_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                pair TEXT NOT NULL,
                setup_name TEXT NOT NULL,
                score INTEGER NOT NULL,
                direction TEXT NOT NULL,
                historical_win_rate REAL NOT NULL,
                historical_trade_count INTEGER NOT NULL,
                historical_profit_factor REAL NOT NULL,
                current_rsi REAL,
                current_stoch_k REAL,
                current_stoch_d REAL,
                bb_position TEXT,
                candle_pattern TEXT,
                h4_bias TEXT,
                reasoning TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                alert_type TEXT DEFAULT 'LEGACY',
                cascade_phase TEXT,
                fan_width_pips REAL,
                fan_delta_5bar REAL,
                bb_width_pct REAL,
                bb_delta_5bar REAL,
                is_retracement INTEGER,
                both_expanding INTEGER,
                both_contracting INTEGER,
                e100_dist_pips REAL,
                story_score INTEGER,
                checklist_score INTEGER
            )
        """)
        # Migration: add new columns to existing DBs
        for _col, _typ in [
            ('user_id','INTEGER'), ('alert_type','TEXT DEFAULT \'LEGACY\''),
            ('cascade_phase','TEXT'), ('fan_width_pips','REAL'), ('fan_delta_5bar','REAL'),
            ('bb_width_pct','REAL'), ('bb_delta_5bar','REAL'), ('is_retracement','INTEGER'),
            ('both_expanding','INTEGER'), ('both_contracting','INTEGER'),
            ('e100_dist_pips','REAL'), ('story_score','INTEGER'), ('checklist_score','INTEGER'),
        ]:
            try: conn.execute(f"ALTER TABLE scout_alerts ADD COLUMN {_col} {_typ}")
            except Exception: pass
        conn.commit()
        # Don't close pooled connections

    def _load_playbook_setups(self):
        """Load ALL quality setups from database (TIM'S UPDATE: 80%+ win rate for broader dataset)."""
        conn = get_trading_forex()
        query = """
            SELECT setup, pair, timeframe, win_rate, trade_count, profit_factor,
                   regime, h4_agrees_win_rate, best_session, best_session_win_rate
            FROM backtest_setup_performance
            WHERE win_rate >= {tc_get("scout.win_rate_elite", 80.0)}
            AND trade_count >= 100
            AND profit_factor > 1.0
            ORDER BY win_rate DESC, profit_factor DESC
        """
        try:
            df = pd.read_sql_query(query, conn)
            self.playbook_setups = df.to_dict('records')

            logger.info(f"Loaded {len(self.playbook_setups)} playbook setups (80%+ win rate)")

            # Group by setup name for easier lookup
            self.setups_by_name = {}
            for setup in self.playbook_setups:
                name = setup['setup']
                if name not in self.setups_by_name:
                    self.setups_by_name[name] = []
                self.setups_by_name[name].append(setup)

            # Log tier breakdown
            tiers = {'80-84%': 0, '85-89%': 0, '90%+': 0}
            for setup in self.playbook_setups:
                wr = setup['win_rate']
                if wr >= 90:
                    tiers['90%+'] += 1
                elif wr >= 85:
                    tiers['85-89%'] += 1
                else:
                    tiers['80-84%'] += 1

            logger.info(f"Playbook tiers: {tiers['80-84%']} base, {tiers['85-89%']} elevated, {tiers['90%+']} elite")

        except Exception as e:
            logger.error(f"Error loading playbook setups: {e}")
            self.playbook_setups = []
            self.setups_by_name = {}

        # Don't close pooled connections

    def _load_scout_thresholds(self):
        """Load scout thresholds from JSON file."""
        threshold_file = os.path.join(current_dir, 'scout_thresholds.json')
        try:
            if os.path.exists(threshold_file):
                with open(threshold_file, 'r') as f:
                    self.scout_thresholds = json.load(f)
                logger.info(f"Loaded {len(self.scout_thresholds)} scout thresholds")
            else:
                logger.warning("Scout thresholds file not found - using default thresholds")
                self.scout_thresholds = {}
        except Exception as e:
            logger.error(f"Error loading scout thresholds: {e}")
            self.scout_thresholds = {}

    async def start(self):
        """Start the trade scout."""
        logger.info("Starting Trade Scout...")
        self.running = True
        self._loop = asyncio.get_event_loop()  # Store for signal handler

        # Start WebSocket server for dashboard alerts
        websocket_task = asyncio.create_task(self._start_websocket_server())

        # Start main scanning loop (M15 — new opportunities).
        # Snipe evaluation is performed by trading_api_routes._watch_checker_loop
        # (5-min cadence, per-user). The legacy scout-side _snipe_monitor_loop and
        # _snipe_fast_check_loop (M1) were removed 2026-04-22 — both had been dead
        # for 14+ days due to user_id=None mismatch against watch_suggestions rows.
        scan_task = asyncio.create_task(self._scan_loop())

        await asyncio.gather(websocket_task, scan_task)

    def stop(self):
        """Stop the trade scout."""
        logger.info("Stopping Trade Scout...")
        self.running = False

    async def _start_websocket_server(self):
        """Start WebSocket server for dashboard communication."""
        if websockets is None:
            logger.warning("WebSocket support not available - alerts will only be stored in database")
            return

        async def handle_client(websocket, path):
            logger.info("Dashboard client connected")
            self.websocket_clients.add(websocket)
            # Send cached market pictures immediately so new clients don't wait 3 min
            try:
                if self._latest_market_pictures:
                    for pair, mkt in self._latest_market_pictures.items():
                        ema = mkt.get('ema', {})
                        summary = {
                            'type': 'market_picture',
                            'data': {
                                'type': 'market_picture',
                                'pair': pair,
                                'reason': 'CACHED',
                                'direction': ema.get('signal', 'neutral'),
                                'fan_direction': ema.get('fan_direction', 'mixed'),
                                'fan_state': ema.get('fan_state', 'unknown'),
                                'separation_pct': ema.get('separation_pct', 0),
                                'velocity': ema.get('separation_velocity', 0),
                                'velocity_trend': ema.get('fan_velocity_trend', 'unknown'),
                                'trend_health': ema.get('trend_health', 0),
                                'reversal_risk': ema.get('reversal_risk', 'unknown'),
                                'rsi': mkt.get('rsi', {}).get('value'),
                                'rsi_zone': mkt.get('rsi', {}).get('zone', 'neutral'),
                                'stoch_zone': mkt.get('stochastic', {}).get('zone', 'neutral'),
                                'ema_narrative': ema.get('narrative', ''),
                                'confluence_narrative': mkt.get('confluence_narrative', ''),
                                'recommended_bias': mkt.get('recommended_bias', 'neutral'),
                            }
                        }
                        await websocket.send(json.dumps(summary))
                    logger.info("Sent %d cached market pictures to new client", len(self._latest_market_pictures))
            except Exception as e:
                logger.debug("Error sending cached data to client: %s", e)
            try:
                await websocket.wait_closed()
            except Exception:
                pass
            finally:
                self.websocket_clients.discard(websocket)
                logger.info("Dashboard client disconnected")

        try:
            start_server = websockets.serve(handle_client, "localhost", 8767)
            await start_server
            logger.info("WebSocket server started on ws://localhost:8767")
        except Exception as e:
            logger.error(f"Failed to start WebSocket server: {e}")
            logger.warning("Continuing without WebSocket support")

        # ── Lightweight HTTP health endpoint on :8768 ──────────────────────────
        # Lets the watchdog use a proper HTTP check instead of raw TCP (which
        # generates spurious 400 Bad Request noise in the WebSocket log)
        try:
            from http.server import HTTPServer, BaseHTTPRequestHandler
            import threading

            class _HealthHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"scout ok")
                def log_message(self, *args):
                    pass  # silence HTTP access log

            _health_srv = HTTPServer(("127.0.0.1", 8768), _HealthHandler)
            threading.Thread(target=_health_srv.serve_forever, daemon=True).start()
            logger.info("Scout health endpoint started on http://127.0.0.1:8768/health")
        except Exception as e:
            logger.warning("Scout health endpoint failed to start: %s", e)

    PAUSE_FILE = "/tmp/scout_paused"

    @property
    def is_paused(self):
        return os.path.exists(self.PAUSE_FILE)

    async def _scan_loop(self):
        """Main scanning loop - runs every 5 minutes."""
        while self.running:
            # ── FRIDAY CLOSE-OUT: Stop scanning near market close ──
            try:
                from datetime import timezone as _tz
                import zoneinfo as _zi
                _now_utc = datetime.now(_tz.utc)
                try:
                    _et = _zi.ZoneInfo("America/New_York")
                    _now_et = _now_utc.astimezone(_et)
                except Exception:
                    _now_et = _now_utc  # fallback, skip check
                if _now_et.weekday() == 4:  # Friday
                    _mins = _now_et.hour * 60 + _now_et.minute
                    if _mins >= 16 * 60:  # 4:00 PM ET
                        logger.warning(
                            "FRIDAY SCOUT SHUTDOWN: %02d:%02d ET — "
                            "no new scans within 60min of market close",
                            _now_et.hour, _now_et.minute,
                        )
                        await asyncio.sleep(300)
                        continue
            except Exception:
                pass  # Don't let timezone logic break the scout
            # ── END FRIDAY CLOSE-OUT ──

            if self.is_paused:
                # Paused = no NEW opportunity scanning, but snipe monitoring continues
                logger.info("Scout PAUSED (snipe checks still active) - running snipe-only scan...")
                try:
                    await self._snipe_only_scan()
                except Exception as e:
                    logger.error(f"Error in snipe-only scan: {e}")
                await asyncio.sleep(300)  # 5 min between snipe checks
                continue
            try:
                self._last_full_scan_ts = time.time()  # snipe monitor uses this to avoid double-checking
                logger.info("Starting market scan... [v2-ordered-fan-fix]")
                start_time = time.time()

                # Flush stale scout entries from queue before new scan
                self._flush_stale_scout_entries()

                # Scan all pairs in parallel
                tasks = [self._scan_pair(pair) for pair in self.pairs]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Process results with detailed logging
                total_alerts = 0
                ema_alerts = 0
                high_separation_pairs = []
                sniper_alerts = 0

                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        import traceback
                        tb = ''.join(traceback.format_exception(type(result), result, result.__traceback__))
                        logger.error(f"Error scanning {self.pairs[i]}: {result}")
                        with open("/tmp/scout_traceback.txt", "a") as _tbf:
                            _tbf.write(f"\n{'='*60}\n{self.pairs[i]} @ {datetime.now()}\n{tb}\n")
                    else:
                        pair_alerts = result or []
                        total_alerts += len(pair_alerts)

                        for alert in pair_alerts:
                            if alert.get('type') == 'ema_separation':
                                ema_alerts += 1
                                if alert.get('elite_boost', False):
                                    high_separation_pairs.append({
                                        'pair': alert['pair'],
                                        'separation_pct': alert['separation_pct'],
                                        'velocity': alert['velocity_class'],
                                        'direction': alert['direction']
                                    })
                            else:
                                sniper_alerts += 1

                elapsed = time.time() - start_time
                logger.info(
                    f"Scan completed: {elapsed:.2f}s | Pairs: {len(self.pairs)} | "
                    f"Total alerts: {total_alerts} (EMA: {ema_alerts}, Sniper: {sniper_alerts}) | "
                    f"Elite separation pairs: {len(high_separation_pairs)}"
                )
                # Write heartbeat so the API always shows the real last-scan time
                try:
                    with open("/tmp/scout_last_scan", "w") as _hb:
                        _hb.write(datetime.now(tz=timezone.utc).isoformat())
                except Exception:
                    pass

                # Periodic WAL checkpoint after each scan cycle.
                # Prevents WAL bloat that causes disk I/O errors under heavy
                # concurrent access (validator cycles + snipe checks + scout).
                try:
                    from db_pool import force_checkpoint
                    force_checkpoint('trading_forex')
                except Exception:
                    pass

                if high_separation_pairs:
                    for pair_data in high_separation_pairs:
                        logger.info(
                            f"Elite separation: {pair_data['pair']} - "
                            f"{pair_data['separation_pct']:.3f}% ({pair_data['velocity']}, {pair_data['direction']})"
                        )

                # Periodic tasks every 6 scan cycles (30 minutes)
                self.scan_cycle_count += 1
                if self.scan_cycle_count % 6 == 0 and self.profile_engine:
                    try:
                        logger.info("Running periodic profile engine maintenance...")
                        self.profile_engine.resolve_pending_findings()

                        # Daily tasks (every 24 hours worth of cycles = 288 cycles)
                        if self.scan_cycle_count % 288 == 0:
                            logger.info("Daily profile rebuild triggered...")
                            self.profile_engine.rebuild_daily()
                            # Purge old cancelled/expired watches
                            try:
                                from Source.agents.watch_manager import purge_old_watches
                                purge_old_watches(days=7)
                            except Exception as pw_e:
                                logger.warning("Watch purge failed: %s", pw_e)
                    except Exception as e:
                        logger.error(f"Error in periodic profile maintenance: {e}")
                
                # PROBLEM 2 FIX: Aggregate scout performance metrics every 20 cycles (~100 minutes)
                if self.scan_cycle_count % 20 == 0:
                    try:
                        from scout_learning_system import aggregate_performance
                        aggregate_performance()
                        logger.info("🎯 Scout performance metrics aggregated")
                    except Exception as e:
                        logger.debug("Scout metrics aggregation: %s", e)

                # Wait until 60 seconds after the next M15 candle close
                # M15 candles close at :00, :15, :30, :45 - scan at :01, :16, :31, :46
                # Target: always scan once per M15 candle, never >15 min between scans
                now = datetime.now()
                total_secs = now.minute * 60 + now.second
                # Next M15 boundary in seconds-of-hour: 0, 900, 1800, 2700
                current_slot = total_secs // 900  # which M15 slot we're in (0-3)
                next_boundary = (current_slot + 1) * 900  # next boundary in secs-of-hour
                # Target = next boundary + 60s (scan 1 min after candle close)
                target_secs = next_boundary + 60
                wait_seconds = target_secs - total_secs
                # Handle hour wrap (e.g., at :59 targeting next hour's :01)
                if wait_seconds <= 0:
                    wait_seconds += 3600
                # If wait is very short (<60s), we just passed a boundary we already
                # scanned for — skip to the NEXT M15 candle instead
                if wait_seconds < 60:
                    wait_seconds += 900
                target_min = (now.minute + (wait_seconds + now.second) // 60) % 60
                logger.info(f"Next scan in {wait_seconds}s ({wait_seconds/60:.1f}m, at :{target_min:02d})")
                await asyncio.sleep(wait_seconds)

            except Exception as e:
                logger.error(f"Error in scan loop: {e}")
                await asyncio.sleep(60)  # Wait 1 minute before retry

    # ── Removed 2026-04-22: _snipe_monitor_loop, _FAST_CONDITION_FIELDS,
    # _snipe_fast_check_loop, _fast_check_active_snipes, _count_active_snipes.
    # All were dead due to user_id=None mismatch in _count_active_snipes (scout
    # instantiated without user_id in serve_ui.py). 15,711 idle iterations over
    # 14 days with 0 escalations. Real snipe evaluation runs in
    # trading_api_routes._watch_checker_loop (per-user, 5-min cadence).
    # _snipe_only_scan (below) retained — called from _scan_loop when paused.

    async def _snipe_only_scan(self):
        """Lightweight scan that ONLY checks active snipes — no new opportunity detection.
        Runs when scout is paused so snipe monitoring never stops."""
        api_key = self._api_key
        if not api_key:
            logger.error("Snipe-only scan: no API key available (check OANDA_API_KEY env var)")
            return

        for pair in self.pairs:
            try:
                with OandaClient(api_key, self.practice_url) as client:
                    candles_data = client.get_candles(pair, granularity="M15", count=800)
                    if isinstance(candles_data, list):
                        candles_list = candles_data
                    elif isinstance(candles_data, dict) and 'candles' in candles_data:
                        candles_list = candles_data['candles']
                    else:
                        continue

                if len(candles_list) < 200:
                    continue

                # Build DataFrame for snipe condition checks
                import pandas as pd
                rows = []
                for c in candles_list:
                    mid = c.get('mid', c)
                    rows.append({
                        'time': c.get('time', ''),
                        'open': float(mid.get('o', mid.get('open', 0))),
                        'high': float(mid.get('h', mid.get('high', 0))),
                        'low': float(mid.get('l', mid.get('low', 0))),
                        'close': float(mid.get('c', mid.get('close', 0))),
                        'volume': int(c.get('volume', 0)),
                    })
                df = pd.DataFrame(rows)
                df = add_enhanced_indicators(df)
                latest_row = df.iloc[-1].copy()

                # Add previous-bar values needed by check_conditions
                if len(df) >= 2:
                    prev_row = df.iloc[-2]
                    latest_row["bb_width_prev"] = prev_row.get("bb_width", latest_row.get("bb_width", 0))
                    # BB squeeze: True if BB width < 0.003 (compressed)
                    latest_row["bb_squeeze"] = bool(latest_row.get("bb_width", 0) < 0.003)

                # ── Compute divergence for snipe-only scan ──
                _candles = []
                for _, row in df.tail(50).iterrows():
                    _candles.append({
                        'close': row['close'],
                        'high': row['high'],
                        'low': row['low'],
                        'open': row['open']
                    })
                
                try:
                    _div_result = {'rsi_bull_div': False, 'rsi_bear_div': False,
                                   'rsi_hidden_bull_div': False, 'rsi_hidden_bear_div': False,
                                   'macd_bull_div': False, 'macd_bear_div': False}
                    if len(_candles) >= 30:
                        _closes = [c['close'] for c in _candles[-50:]]
                        _rsi_series = _compute_rsi_series(_closes)
                        _macd_series = _compute_macd_hist_series(_closes)
                        _div_result = _detect_divergence_at_current(_closes, _rsi_series, _macd_series)
                    # Inject into latest_row so score_v4 can read it
                    latest_row = latest_row.copy()
                    for _dk, _dv in _div_result.items():
                        latest_row[_dk] = _dv
                except Exception as _div_err:
                    # 2026-04-24: upgraded — silent = score_v4 missing div flags.
                    logger.warning(f"[{pair}] Divergence FAILED (snipe-only scan): {type(_div_err).__name__}: {_div_err}")

                bull_score = score_v4(latest_row, 'buy')
                bear_score = score_v4(latest_row, 'sell')

                # Compute market picture for snipe condition evaluation
                candle_dicts = [{'time': r['time'], 'open': r['open'], 'high': r['high'],
                                 'low': r['low'], 'close': r['close']} for r in rows]
                from backtester.ema_separation import generate_market_picture
                mkt_picture = generate_market_picture(pair, candle_dicts)

                # Build minimal story for snipe checks
                from market_story import read_market_story
                story = read_market_story(pair, candle_dicts, mkt_picture)

                # Check snipes
                snipe_triggers = self._check_snipes_for_pair(
                    pair=pair,
                    bull_score=bull_score,
                    bear_score=bear_score,
                    indicators=latest_row,
                    market_picture=mkt_picture,
                    market_snapshot=None,
                    market_story=story,
                )
                for snipe in snipe_triggers:
                    await self._broadcast_alert(snipe)
                if snipe_triggers:
                    logger.info(f"🎯 [{pair}] {len(snipe_triggers)} snipe(s) triggered (PAUSED MODE)")

            except Exception as e:
                # 2026-04-24: upgraded — silent = whole pair scan drops, no alerts for this pair.
                logger.warning(f"Snipe-only scan FAILED for {pair}: {type(e).__name__}: {e} (no snipe alerts this cycle)")

    async def _scan_pair(self, pair: str) -> List[Dict]:
        """Scan a single pair by reading the market like a trader.

        Architecture (mirrors Position Guardian's contextual approach):
          Layer 1: Trend Narrative (EMA fan) - "What's the story?"
          Layer 2: Price Structure (E100 + candles + wicks) - "What are candles doing?"
          Layer 3: Momentum Confirmation (RSI+Stoch+MACD as ONE read) - "Does momentum agree?"
          Layer 4: Historical Validation - overlay backtest DB + profile engine on the thesis
          v4 Score: reported as supporting data, NOT used as a gate
        """
        _scan_start = time.time()

        # ── PER-PAIR SESSION GATING (2026-05-10 replacement) ──
        # Previously: blanket Sunday-≥17 and 22–02 ET blocks returned [] for ALL pairs
        # even though Sydney (17–02 ET) + Tokyo (19–04 ET) sessions are open and
        # JPY/AUD/NZD pairs are in their prime trading windows. The blanket block
        # also used hardcoded EST (-5) which misfires by 1 hour during EDT (-4).
        # Now: defer to market_sessions.get_session_quality which honors PAIR_SESSIONS
        # mapping (Sydney/Tokyo/London/NY → pair → quality 0.0–1.0 with DST-aware
        # pytz tz). Quality < 0.5 = no major sessions active for this pair = skip.
        # The original 4 overnight Sunday losses (3/2/2026, 3:47–5:32 AM EST) sit in
        # quality<0.5 for affected pairs anyway, so the spirit of the block is kept.
        try:
            from market_sessions import get_session_quality as _gsq
            _pair_quality = _gsq(pair)
        except Exception:
            _pair_quality = 0.5  # fail-open if market_sessions unavailable
        if _pair_quality < 0.5:
            return []

        # COOLDOWN CHECK: skip pair if it just had a cycle (hold/reject)
        cooldown_until = self._pair_cooldowns.get(pair, 0)
        if time.time() < cooldown_until:
            remaining = int(cooldown_until - time.time())
            logger.debug(f"[{pair}] COOLDOWN: skipping ({remaining}s remaining)")
            flight.record(FlightStage.SCOUT_SCAN, pair=pair,
                         duration_ms=(time.time() - _scan_start) * 1000,
                         data={
                             "story_score": 0, "entry_type": "cooldown",
                             "fan_state": "cooldown", "has_opportunity": False,
                         },
                         note=f"cooldown | {remaining}s remaining")
            return []

        try:
            api_key = self._api_key

            with OandaClient(api_key, self.practice_url) as client:
                candles_data = client.get_candles(pair, granularity="M15", count=800)

                if isinstance(candles_data, list):
                    candles_list = candles_data
                elif isinstance(candles_data, dict) and 'candles' in candles_data:
                    candles_list = candles_data['candles']
                else:
                    logger.warning(f"No candle data for {pair}")
                    return []

                if not candles_list:
                    logger.warning(f"Empty candle data for {pair}")
                    return []

                # Convert to DataFrame (needed for v4 score + indicators for snipe checks)
                df = self._candles_to_dataframe(candles_list)

                if len(df) < 200:
                    logger.warning(f"Insufficient data for {pair}: {len(df)} candles")
                    return []

                # Calculate indicators (still needed for dashboard, snipe checks, v4 validation)
                df = self._calculate_indicators(df)
                df = add_enhanced_indicators(df)
                df = detect_all_patterns(df)

                # ── H4 BIAS: fetch H4 candles and compute bias against EMA21 ──
                # FIX: scout never fetched H4 data — h4_bias was always 'unknown'.
                # This mirrors the logic in agents/wrappers.py (lines 720-744).
                try:
                    _h4_data = client.get_candles(pair, granularity="H4", count=50)
                    _h4_list = _h4_data if isinstance(_h4_data, list) else _h4_data.get('candles', [])
                    if len(_h4_list) >= 10:
                        import pandas as _pd_h4
                        _h4_rows = []
                        for _c in _h4_list:
                            _mid = _c.get('mid', _c)
                            _h4_rows.append({'close': float(_mid.get('c', _mid.get('close', 0)))})
                        _h4_df = _pd_h4.DataFrame(_h4_rows)
                        _h4_close = _h4_df['close'].iloc[-1]
                        _h4_ema21 = _h4_df['close'].ewm(span=21).mean().iloc[-1]
                        if _h4_close > _h4_ema21 * 1.001:
                            _h4_bias = 'bull'
                        elif _h4_close < _h4_ema21 * 0.999:
                            _h4_bias = 'bear'
                        else:
                            _h4_bias = 'range'
                        df['h4_bias'] = _h4_bias
                        logger.debug(f"[{pair}] H4 bias: {_h4_bias} (close={_h4_close:.5f} ema21={_h4_ema21:.5f})")
                    else:
                        df['h4_bias'] = 'none'
                except Exception as _h4_err:
                    logger.debug(f"[{pair}] H4 fetch failed: {_h4_err}")
                    df['h4_bias'] = 'none'

                latest_row = df.iloc[-1].copy()
                # Add previous-bar values needed by check_conditions
                if len(df) >= 2:
                    _prev = df.iloc[-2]
                    latest_row["bb_width_prev"] = _prev.get("bb_width", latest_row.get("bb_width", 0))
                    latest_row["bb_squeeze"] = bool(latest_row.get("bb_width", 0) < 0.003)

                # ── Compute divergence for ALL paths (not just Path D) ──
                _candles = []
                for _, row in df.tail(50).iterrows():
                    _candles.append({
                        'close': row['close'],
                        'high': row['high'],
                        'low': row['low'],
                        'open': row['open']
                    })
                
                try:
                    _div_result = {'rsi_bull_div': False, 'rsi_bear_div': False,
                                   'rsi_hidden_bull_div': False, 'rsi_hidden_bear_div': False,
                                   'macd_bull_div': False, 'macd_bear_div': False}
                    if len(_candles) >= 30:
                        _closes = [c['close'] for c in _candles[-50:]]
                        _rsi_series = _compute_rsi_series(_closes)
                        _macd_series = _compute_macd_hist_series(_closes)
                        _div_result = _detect_divergence_at_current(_closes, _rsi_series, _macd_series)
                    # Inject into latest_row so score_v4 can read it
                    latest_row = latest_row.copy()
                    for _dk, _dv in _div_result.items():
                        latest_row[_dk] = _dv
                except Exception as _div_err:
                    # 2026-04-24: upgraded — silent = score_v4 missing div flags.
                    logger.warning(f"[{pair}] Divergence FAILED (_scan_pair): {type(_div_err).__name__}: {_div_err}")

                # Chart patterns for market snapshot
                chart_pattern_results = []
                fib_data = {}
                try:
                    from backtester.chart_patterns import detect_all_chart_patterns, find_fibonacci_reactions
                    chart_pattern_results = detect_all_chart_patterns(df, lookback=100)
                    fib_reactions = find_fibonacci_reactions(df, lookback=100)
                    if fib_reactions:
                        fib_data = {'reactions': fib_reactions}
                except Exception as e:
                    # 2026-04-24: upgraded — silent = fib/patterns empty,
                    # setup classifier misses pattern context.
                    logger.warning(f"Chart pattern detection FAILED for {pair}: {type(e).__name__}: {e}")

                # Setup classifier (still useful as metadata)
                classified_setups = []
                classified_best = []
                try:
                    from setup_classifier import classify_setups, get_best_setups
                    ind_dict = {
                        'rsi': latest_row.get('rsi', latest_row.get('RSI', 50)),
                        'stoch_k': latest_row.get('stoch_k', 50),
                        'stoch_d': latest_row.get('stoch_d', 50),
                        'adx': latest_row.get('adx', latest_row.get('ADX', 25)),
                        'macd_value': latest_row.get('macd', latest_row.get('MACD', 0)),
                        'macd_signal': latest_row.get('macd_signal', latest_row.get('MACD_signal', 0)),
                        'macd_hist': latest_row.get('macd_hist', latest_row.get('MACD_hist', 0)),
                        'bb_upper': latest_row.get('bb_upper', latest_row.get('BB_upper', 0)),
                        'bb_lower': latest_row.get('bb_lower', latest_row.get('BB_lower', 0)),
                        'bb_mid': latest_row.get('bb_mid', latest_row.get('BB_mid', 0)),
                        'bb_width': latest_row.get('bb_width', latest_row.get('BB_width', 0)),
                        'close': latest_row.get('close', 0),
                        'sma50': latest_row.get('sma50', latest_row.get('SMA_50', 0)),
                        'sma100': latest_row.get('sma100', latest_row.get('SMA_100', 0)),
                        'sar': latest_row.get('sar', latest_row.get('SAR', 0)),
                        'cci': latest_row.get('cci', latest_row.get('CCI', 0)),
                        'ema_21': latest_row.get('ema_21', latest_row.get('EMA_21', 0)),
                        'ema_55': latest_row.get('ema_55', latest_row.get('EMA_55', 0)),
                        'ema_100': latest_row.get('ema_100', latest_row.get('EMA_100', 0)),
                        'atr': latest_row.get('atr', latest_row.get('ATR', 0)),
                        'adx_slope': latest_row.get('adx_slope', 0),
                    }
                    candle_cols = [c for c in df.columns if c in [
                        'hammer', 'inverted_hammer', 'shooting_star', 'doji', 'dragonfly_doji',
                        'gravestone_doji', 'bullish_engulfing', 'bearish_engulfing', 'morning_star',
                        'evening_star', 'three_white_soldiers', 'three_black_crows',
                        'tweezer_bottom', 'tweezer_top', 'piercing_line', 'dark_cloud',
                        'marubozu_bull', 'marubozu_bear', 'spinning_top'
                    ]]
                    candle_dict = {c: bool(latest_row.get(c, False)) for c in candle_cols}
                    current_regime_for_classifier = self._get_current_regime(latest_row)
                    classified_setups = classify_setups(
                        indicators=ind_dict, candle_patterns=candle_dict,
                        chart_patterns=chart_pattern_results,
                        regime=current_regime_for_classifier, fib_data=fib_data,
                    )
                    if classified_setups:
                        classified_best = get_best_setups(classified_setups, min_confidence=0.60, max_results=3)
                        if classified_best:
                            setup_strs = [f"{s['setup']}({s['name']},{s['direction']},{s['confidence']:.0%})" for s in classified_best]
                            logger.info("[%s] Active setups: %s", pair, ", ".join(setup_strs))
                except Exception as e:
                    # 2026-04-24: upgraded — silent = classified_best empty,
                    # scout alerts miss setup labels.
                    logger.warning(f"Setup classifier FAILED for {pair}: {type(e).__name__}: {e}")

                # ══════════════════════════════════════════════════════════
                # PIPELINE MODEL: Sniper FINDS → Thesis CONFIRMS
                #
                # Step 1: Sniper score (84.6% WR backtested) is the TRIGGER
                # Step 2: Market story/thesis CONFIRMS the direction
                # Step 3: Historical validation overlays evidence
                #
                # The sniper catches extremes. The thesis confirms the
                # trend context supports the entry. They complement,
                # not contradict.
                # ══════════════════════════════════════════════════════════

                # Build candle list for market_story (needs dicts with OHLCV)
                candle_dicts = self._dataframe_to_candles(df)

                # Generate market picture (used by story reader + dashboard)
                mkt_picture = generate_market_picture(pair, candle_dicts)
                ema_signal = mkt_picture.get('ema', {})

                # Read the full market story (Layers 1-3) - always computed
                # for dashboard, snipe checks, and thesis confirmation
                from market_story import read_market_story
                story = read_market_story(pair, candle_dicts, mkt_picture)

                # ── CONSOLIDATION DETECTION ──
                # Markets in consolidation = no edge. Skip entirely.
                # Consolidation = low ADX + tight BB + flat/mixed EMAs
                _adx = latest_row.get('adx', latest_row.get('ADX', 25))
                try:
                    _bb_upper = float(latest_row['bb_upper'])
                    _bb_lower = float(latest_row['bb_lower'])
                    _close = float(latest_row['close'])
                    # BB width as % of price — consolidation = tight bands
                    _bb_width = ((_bb_upper - _bb_lower) / _close) if _close else 0
                except (KeyError, TypeError, ValueError):
                    _bb_width = 999  # Unknown = don't flag as consolidation
                _fan_state = ema_signal.get('fan_state', 'unknown')
                _fan_dir = ema_signal.get('fan_direction', 'neutral')
                _trend_health = mkt_picture.get('trend_health', 50)

                # An ordered fan (bullish/bearish direction) that is contracting
                # is a RETRACEMENT setup, not consolidation. Only block when
                # the fan direction is mixed/neutral (true chop, no trend skeleton).
                _fan_ordered_for_consol = _fan_dir in ('bullish', 'bearish')
                _is_consolidating = (
                    _adx < 20 and
                    _bb_width < 0.005 and
                    _fan_state in ('stable', 'mixed', 'just_crossed', 'contracting') and
                    not _fan_ordered_for_consol and  # ordered contracting fan = retracement, not consolidation
                    (_fan_dir in ('mixed', 'neutral') or _trend_health < 30)
                )

                if _is_consolidating:
                    logger.info(
                        f"💤 [{pair}] CONSOLIDATION: ADX={_adx:.1f} BB_width={_bb_width:.4f} "
                        f"Fan={_fan_dir} {_fan_state} Health={_trend_health} — no edge, skipping"
                    )
                    flight.record(FlightStage.SCOUT_SCAN, pair=pair,
                                 duration_ms=(time.time() - _scan_start) * 1000,
                                 data={
                                     "story_score": story['opportunity_score'],
                                     "entry_type": story.get('entry_type', 'none'),
                                     "fan_state": _fan_state, "fan_direction": _fan_dir,
                                     "v4_buy": 0, "v4_sell": 0,
                                     "has_opportunity": False, "consolidating": True,
                                     "adx": _adx, "bb_width": round(_bb_width, 5),
                                 },
                                 note=f"consolidation | ADX={_adx:.0f} BB={_bb_width:.4f}")
                    return []

                # ── SNIPER SCORE (secondary — retracement/reversal signal) ──
                params = TF_PARAMS.get(self.timeframe, TF_PARAMS.get("M15", TF_PARAMS["H1"]))
                bull_score, bear_score = score_v4(latest_row, params)
                max_score = max(bull_score, bear_score)
                sniper_threshold = params.get("threshold", 12)
                sniper_triggered = max_score >= sniper_threshold

                alerts = []
                fan_state = ema_signal.get('fan_state', 'unknown')
                fan_direction = ema_signal.get('fan_direction', 'neutral')

                # ══════════════════════════════════════════════════════════
                # V4: EXPANSION-FIRST SEARCH
                #
                # Scout searches like the validator's checklist:
                #   1. Is expansion happening? (PRIMARY — no sniper needed)
                #   2. Is a retracement setting up the next expansion?
                #   3. Did sniper fire? (retracement/reversal signal, NOT a gate)
                #
                # Sniper = "market is stretched" = retracement or reversal
                #   - In expansion → retracement warning (watch for re-entry)
                #   - No expansion → potential reversal (watch for cross)
                #
                # Direction comes ONLY from the Validator.
                # ══════════════════════════════════════════════════════════

                # ── ATR minimum ──
                _atr_val = float(latest_row.get('atr', latest_row.get('ATR', 0)))
                _atr_mins = {
                    'EUR_USD': 0.0005, 'GBP_USD': 0.0007, 'USD_JPY': 0.06,
                    'AUD_USD': 0.0004, 'NZD_USD': 0.0004, 'USD_CAD': 0.0005,
                    'USD_CHF': 0.0005, 'EUR_GBP': 0.0004, 'EUR_JPY': 0.08,
                    'GBP_JPY': 0.10, 'EUR_CHF': 0.00038, 'EUR_AUD': 0.0006,
                    'AUD_JPY': 0.06,
                }
                _atr_min_val = _atr_mins.get(pair, 0.0004)

                # ── Measure ALL thesis conditions via shared utility ──
                _pip_sz = 0.01 if 'JPY' in pair else 0.0001
                _thesis = compute_thesis_measurements(df, _pip_sz, fan_state, fan_direction)

                # Unpack with safe defaults — downstream comparisons like
                # `_bb_delta_5bar > 0.0004` would TypeError on None.
                _bb_delta_5bar = _thesis["bb_delta_5bar"] if _thesis["bb_delta_5bar"] is not None else 0.0
                _bb_delta_20bar = _thesis["bb_delta_20bar"] if _thesis["bb_delta_20bar"] is not None else 0.0
                _bb_expanding = _thesis["bb_expanding"] or False
                _bb_width_now = _thesis["bb_width_now"] if _thesis["bb_width_now"] is not None else 0.0
                _fan_delta_5bar = _thesis["fan_delta_5bar"] if _thesis["fan_delta_5bar"] is not None else 0.0
                _fan_delta_20bar = _thesis["fan_delta_20bar"] if _thesis["fan_delta_20bar"] is not None else 0.0
                _fan_expanding = _thesis["fan_expanding"] or False
                _fan_accelerating = _thesis["fan_accelerating"] or False
                _fan_width_now = _thesis["fan_width_now"] if _thesis["fan_width_now"] is not None else 0.0
                _candles_moving_away = _thesis["candles_moving_away"] or False
                _e100_dist_pips = _thesis["e100_dist_pips"] if _thesis["e100_dist_pips"] is not None else 0.0
                _separation_accelerating = _thesis["separation_accelerating"] or False
                _e100_dist_history = _thesis["e100_dist_history"] or []
                _recent_cross = _thesis["recent_cross"] or False
                _cross_bars_ago = _thesis["cross_bars_ago"]
                _cross1_direction = _thesis["cross1_direction"]
                _cross2_detected = _thesis["cross2_detected"] or False
                _cross2_bars_ago = _thesis["cross2_bars_ago"]
                _cross2_direction = _thesis["cross2_direction"]
                _dual_cross_cascade = _thesis["dual_cross_cascade"] or False
                _cascade_direction = _thesis["cascade_direction"]
                _e55_dist_pips = _thesis["e55_dist_pips"] if _thesis["e55_dist_pips"] is not None else 0.0
                _rsi_now = _thesis["rsi_now"] if _thesis["rsi_now"] is not None else 50.0
                _rsi_recovery_ok = _thesis["rsi_recovery_ok"] if _thesis["rsi_recovery_ok"] is not None else True
                _rsi_was_extreme = _thesis["rsi_was_extreme"] or False
                _rsi_extreme_val = _thesis["rsi_extreme_val"]
                _rsi_healthy = _thesis["rsi_healthy"] if _thesis["rsi_healthy"] is not None else False
                _stoch_k_now = _thesis["stoch_k_now"] if _thesis["stoch_k_now"] is not None else 50.0
                _stoch_d_now = _thesis["stoch_d_now"] if _thesis["stoch_d_now"] is not None else 50.0
                _stoch_bull_cross = _thesis["stoch_bull_cross"] or False
                _stoch_bear_cross = _thesis["stoch_bear_cross"] or False
                _rsi_bull_divergence = _thesis["rsi_bull_divergence"] or False
                _rsi_bear_divergence = _thesis["rsi_bear_divergence"] or False
                _momentum_candles = _thesis["momentum_candles"] or False
                _candles_correct_side = _thesis["candles_correct_side"] or False
                _reversal_candle_at_ema = _thesis["reversal_candle_at_ema"] or False
                _reversal_candle_ema_level = _thesis["reversal_candle_ema_level"]
                _reversal_candle_direction = _thesis["reversal_candle_direction"]
                _is_retracement = _thesis["is_retracement"] or False
                _is_retracement_forming = _thesis["is_retracement_forming"] or False
                _retracement_type = _thesis["retracement_type"]
                _was_expanding_recently = _thesis["was_expanding_recently"] or False
                _peak_fan_width = _thesis["peak_fan_width"] if _thesis["peak_fan_width"] is not None else 0.0
                _candles_holding = _thesis["candles_holding"] or False
                _bb_re_expanding = _thesis["bb_re_expanding"] or False
                _tested_e55 = _thesis["tested_e55"] or False
                _tested_e100 = _thesis["tested_e100"] or False
                _fan_flip_detected = _thesis["fan_flip_detected"] or False
                _fan_flip_direction = _thesis["fan_flip_direction"]
                _checklist = _thesis["checklist"] or {}
                _checklist_score = _thesis["checklist_score"] or 0
                _price_val = float(latest_row.get('close', 0))

                # ── Flight: cascade detection result (preserves observability) ──
                try:
                    flight.record(FlightStage.SCOUT_SCAN, pair=pair, data={
                        'substage': 'cascade_detect',
                        'cross1': _recent_cross, 'cross1_dir': _cross1_direction,
                        'cross1_bars': _cross_bars_ago,
                        'cross2': _cross2_detected, 'cross2_dir': _cross2_direction,
                        'cross2_bars': _cross2_bars_ago,
                        'dual_cross_cascade': _dual_cross_cascade,
                        'cascade_direction': _cascade_direction,
                    }, note=f"cascade={'YES '+_cascade_direction if _dual_cross_cascade else 'no'}")
                except Exception:
                    pass

                # ── Flight: retracement detection result (preserves observability) ──
                try:
                    flight.record(FlightStage.SCOUT_SCAN, pair=pair, data={
                        'substage': 'retracement_detect',
                        'is_retracement': _is_retracement,
                        'is_retracement_forming': _is_retracement_forming,
                        'retracement_type': _retracement_type,
                        'bb_re_expanding': _bb_re_expanding,
                        'tested_e55': _tested_e55,
                        'tested_e100': _tested_e100,
                        'peak_fan_width': round(_peak_fan_width, 4),
                        'was_expanding': _was_expanding_recently,
                        'stoch_bull_cross': _stoch_bull_cross,
                        'stoch_bear_cross': _stoch_bear_cross,
                        'rsi_bull_div': _rsi_bull_divergence,
                        'rsi_bear_div': _rsi_bear_divergence,
                        'reversal_candle_at_ema': _reversal_candle_at_ema,
                    }, note=f"retrace={_retracement_type or 'none'} forming={_is_retracement_forming} bb_reexp={_bb_re_expanding}")
                except Exception:
                    pass

                # ── Logger: preserves scout-specific retracement notifications ──
                if _retracement_type == 'e55_shallow':
                    logger.info(
                        f"\U0001f4d0 [{pair}] E55 SHALLOW RETRACEMENT: tested E55, fan ordered, "
                        f"BBs {'re-expanding' if _bb_re_expanding else 'expanding'} — trend continuation re-entry"
                    )
                elif _retracement_type == 'e100_deep':
                    logger.info(
                        f"\U0001f3af [{pair}] E100 DEEP RETRACEMENT: price crossed E100, "
                        f"fan_state={fan_state}, BBs {'re-expanding' if _bb_re_expanding else 'expanding'} — "
                        f"E100 retest entry zone"
                    )
                elif _retracement_type and 'forming' in _retracement_type:
                    _ema_level = 'e100' if 'e100' in _retracement_type else 'e55'
                    _signals_str = []
                    if (fan_direction == 'bullish' and _stoch_bull_cross) or (fan_direction == 'bearish' and _stoch_bear_cross):
                        _signals_str.append(f"stoch_cross(K={_stoch_k_now:.0f})")
                    if (fan_direction == 'bullish' and _rsi_bull_divergence) or (fan_direction == 'bearish' and _rsi_bear_divergence):
                        _signals_str.append("RSI_divergence")
                    if _reversal_candle_at_ema and _reversal_candle_direction == fan_direction:
                        _signals_str.append(f"reversal_candle@{_reversal_candle_ema_level}")
                    logger.info(
                        f"\U0001f3a3 [{pair}] RETRACEMENT FORMING at {_ema_level.upper()}: "
                        f"fan={fan_direction} ordered, price at {_ema_level} ({_e55_dist_pips:.1f}p/{_e100_dist_pips:.1f}p) | "
                        f"Signals: {', '.join(_signals_str)} | "
                        f"BB={'contracting(expected)' if not _bb_expanding else 'expanding'} — "
                        f"fishing line entry forming"
                    )

                # ══════════════════════════════════════════════════════════
                # CLASSIFY ALERT — Expansion first, sniper second
                # ══════════════════════════════════════════════════════════

                has_opportunity = False
                alert_type = None

                # ── FAN STRUCTURE CHECK ──
                # fan_direction is already computed by ema_signal: 'bullish', 'bearish', or 'neutral'/'mixed'
                # 'bullish' = E21>E55>E100 (ordered bull), 'bearish' = E21<E55<E100 (ordered bear)
                # Contracting but still ordered = retracement setup, NOT a dead move.
                _fan_still_ordered = fan_direction in ('bullish', 'bearish')

                # ── Pair quality filter (from labeled chart analysis 2026-03-18) ──
                # USD/JPY 0%WR, USD/CAD 0%WR, USD/CHF 0%WR, NZD/USD 0%WR
                # on labeled training data — these pairs require STRONGER signal
                # before generating a cycle (they tend to range / chop more).
                # Weak pairs: require CRITERIA_MET (not just EARLY_WARNING).
                # Strong pairs (AUD/JPY 64%WR, EUR/AUD 69%WR, EUR/JPY 57%WR): normal.
                _WEAK_PAIRS = {'USD_JPY', 'USD_CAD', 'USD_CHF', 'NZD_USD', 'GBP_USD'}

                # Load user's promoted snipe list — these pairs get relaxed quality gates
                _snipe_list_pairs = set()
                try:
                    from setup_revenue import SetupRevenueTracker
                    for _sl in SetupRevenueTracker().get_snipe_list(self._user_id):
                        _snipe_list_pairs.add(_sl.get("pair", ""))
                except Exception:
                    pass

                # Minimum quality gate
                if _atr_val < _atr_min_val:
                    logger.info(f"\U0001f4a4 [{pair}] BLOCKED: ATR {_atr_val:.5f} < min {_atr_min_val:.5f}")

                elif fan_state in ("peaked", "contracting") and not _is_retracement and not _is_retracement_forming and not _fan_still_ordered:
                    # DEAD MOVE BLOCK: fan peaked/contracting AND fan structure already broke down.
                    # Only block when EMAs are actually disordered (E21/E55/E100 tangled).
                    # If fan is still ORDERED (E21>E55>E100 bull, or E21<E55<E100 bear), it's a
                    # RETRACEMENT — this is Tim's primary setup (counter-trend into peaked fan).
                    # Never block an ordered fan just because it's contracting.
                    logger.info(f"💀 [{pair}] BLOCKED: fan_state={fan_state}, fan disordered — expansion truly over")

                elif _e100_dist_pips < max(5, _atr_val / _pip_sz * 0.4) and not _is_retracement and not _is_retracement_forming and not _recent_cross and not (_bb_expanding and _bb_delta_5bar > 0.0004) and not (fan_state in ('contracting', 'peaked') and _was_expanding_recently):
                    # CHOP ZONE: price too close to E100 with no cross, no retracement setup,
                    # and no BB expansion (BB expanding + price breaking away = not chop anymore).
                    # Threshold is ATR-relative: max(5p, ATR×0.4) so EUR/CHF and other low-vol
                    # pairs get appropriate sensitivity instead of one-size-fits-all 5p minimum.
                    # NOT triggered when fan was recently expanding + now contracting + price at E100
                    # — that's Tim's counter-trend retracement setup, not chop.
                    _chop_threshold = max(5, _atr_val / _pip_sz * 0.4)
                    logger.info(f"\U0001f4a4 [{pair}] BLOCKED: E100 dist {_e100_dist_pips:.1f}p < {_chop_threshold:.1f}p (chop zone, no cross)")

                else:
                    # ── PHASE 2.5 DETECTION: E21×E55 cross + price near/on E100 ──
                    # This is the early fan entry zone. Price on E100 is the BUY/SELL zone,
                    # not a chop block. E55/E100 gap being small is Phase 2.5 normal.
                    _is_phase25 = (
                        _recent_cross and
                        _e100_dist_pips < 10 and      # price near E100 = retest zone
                        _checklist_score >= 3          # at least cross + some forming momentum
                    )

                    # ── BB SQUEEZE DETECTION: Extended tight BBs now beginning to expand ──
                    # EUR/CHF setup: 10+ bars of squeeze → fan flip → explosive breakout
                    _bb_squeeze_breakout = False
                    _squeeze_bar_count = 0
                    try:
                        if len(df) >= 15 and 'bb_upper' in df.columns and 'bb_lower' in df.columns:
                            _bb_widths = [float(df.iloc[_si]['bb_upper']) - float(df.iloc[_si]['bb_lower']) for _si in range(max(len(df)-15, 0), len(df))]
                            _min_bb = min(_bb_widths[:-3]) if _bb_widths[:-3] else 0
                            _current_bb = _bb_widths[-1]
                            _tight_bars = sum(1 for w in _bb_widths[:-3] if w <= _min_bb * 1.3)
                            _squeeze_bar_count = _tight_bars
                            # Squeeze = 6+ bars of tight bands now suddenly expanding
                            _bb_squeeze_breakout = (
                                _tight_bars >= 6 and
                                _current_bb > _min_bb * 1.5 and  # current BB 50% wider than squeeze min
                                _bb_expanding                     # currently still expanding
                            )
                    except (ValueError, TypeError, IndexError):
                        pass

                    # ── PRIORITY 0: CASCADE FORMING — DUAL-CROSS ENTRY ──
                    # Tim's PRIMARY entry: E21×E55 crossed, E21×E100 crossed, candles separating,
                    # BBs opening. This is the "fishing line" — predictive, not reactive.
                    # Enter at step 3-4 (candle separation after second cross), not step 6 (full fan).
                    if _dual_cross_cascade and not has_opportunity:
                        _cascade_has_separation = _separation_accelerating and _e100_dist_pips >= 3
                        _cascade_bb_opening = _bb_expanding or _bb_delta_5bar > 0
                        _cascade_fan_floor = _fan_width_now >= 0.08  # not noodling
                        if (_cascade_has_separation or (_cascade_bb_opening and _candles_moving_away)) and _cascade_fan_floor:
                            has_opportunity = True
                            alert_type = 'CRITERIA_MET'
                            logger.info(
                                f"🎣 [{pair}] CASCADE FORMING — dual-cross {_cascade_direction.upper()} | "
                                f"Cross1 (E21×E55) {_cross_bars_ago}b ago, Cross2 (E21×E100) {_cross2_bars_ago}b ago | "
                                f"Separation={_e100_dist_pips:.1f}p BB={'expanding' if _bb_expanding else 'opening'} | "
                                f"Fan={_fan_width_now:.3f}% SepAccel={'Y' if _separation_accelerating else 'N'} | "
                                f"Checklist {_checklist_score}/11 | [FRESH CASCADE ENTRY]"
                            )
                            try:
                                flight.record(FlightStage.SCOUT_ALERT, pair=pair, data={
                                    'substage': 'cascade_forming',
                                    'cascade_direction': _cascade_direction,
                                    'cross1_bars_ago': _cross_bars_ago,
                                    'cross2_bars_ago': _cross2_bars_ago,
                                    'e100_dist_pips': round(_e100_dist_pips, 1),
                                    'bb_expanding': _bb_expanding,
                                    'checklist_score': _checklist_score,
                                }, note=f"CASCADE FORMING {_cascade_direction} sep={_e100_dist_pips:.1f}p")
                            except Exception:
                                pass

                    # ── PRIORITY 1: FAN FLIP + EXPANSION ──
                    # New trend starting: ordered fan in direction A flipped to direction B.
                    # Catches GBP/JPY (bullish→bearish flip +71p) and EUR/CHF (tangled→bearish +48p).
                    if _fan_flip_detected and not has_opportunity:
                        has_opportunity = True
                        alert_type = 'CRITERIA_MET'
                        logger.info(
                            f"🔄 [{pair}] FAN FLIP — new {_fan_flip_direction.upper()} trend forming | "
                            f"Fan was opposite for 5+ bars, now re-ordered + expanding | "
                            f"Width={_fan_width_now:.4f}% Δ5={_fan_delta_5bar:+.5f} | "
                            f"BB={'expanding' if _bb_expanding else 'flat'} E100={_e100_dist_pips:.1f}p | "
                            f"[v3-fan-flip]"
                        )

                    # ── PRIORITY 2: PHASE 2.5 — E21×E55 CROSS WITH PRICE NEAR E100 ──
                    # Tim's PRIMARY entry zone. Cross happened, price retesting E100.
                    # E100 becomes support (long) or resistance (short).
                    # Earlier in the thesis = better R:R. DO NOT skip this for expansion.
                    if _is_phase25 and not has_opportunity:
                        has_opportunity = True
                        alert_type = 'EARLY_WARNING'  # Demoted: single cross, needs 2nd for CRITERIA_MET
                        logger.info(
                            f"⚡ [{pair}] PHASE 2.5 (WATCHING): E21×E55 cross {_cross_bars_ago} bars ago | "
                            f"Price on E100 retest ({_e100_dist_pips:.1f}p) = IDEAL ENTRY ZONE | "
                            f"Checklist {_checklist_score}/10 | Fan={'Y' if _fan_expanding else 'forming'}"
                        )

                    # ── PRIORITY 3: BB SQUEEZE BREAKOUT ──
                    # Extended BB squeeze (6+ bars tight) now expanding = coiled spring releasing.
                    # EUR/CHF pattern: 10h squeeze → bearish fan flip → 100p breakdown.
                    elif _bb_squeeze_breakout and not has_opportunity and _fan_width_now >= 0.08:
                        has_opportunity = True
                        alert_type = 'CRITERIA_MET'
                        logger.info(
                            f"🔥 [{pair}] BB SQUEEZE BREAKOUT: {_squeeze_bar_count} tight bars → now expanding | "
                            f"Fan={'expanding' if _fan_expanding else 'forming'} Cross={'Y' if _recent_cross else 'N'} | "
                            f"E100={_e100_dist_pips:.1f}p | Checklist {_checklist_score}/10"
                        )

                    # ── PRIORITY 4: EXPANSION HAPPENING NOW ──
                    # Fan + BB expanding + candles moving away. Later entry but still valid.
                    # Phase 2.5 missed (price already past E100) — catch the continuation.
                    # NOTE: conditions must be in the elif guard, NOT inside the block.
                    # If they were inside, the elif would always match (not has_opportunity=True)
                    # and swallow Priorities 4-6 unreachable.
                    elif (not has_opportunity and
                          _fan_expanding and _bb_expanding and
                          _candles_moving_away and _e100_dist_pips >= 10 and
                          _checklist_score >= 4):
                        has_opportunity = True
                        alert_type = 'EARLY_WARNING'  # Demoted: no dual-cross requirement
                        logger.info(
                            f"\U0001f525 [{pair}] EXPANSION IN PROGRESS (watching): checklist {_checklist_score}/10 | "
                            f"Fan Δ5={_fan_delta_5bar:+.5f} BB Δ5={_bb_delta_5bar:+.5f} | "
                            f"E100={_e100_dist_pips:.1f}p RSI={_rsi_now:.0f} | "
                            f"Momentum={'Y' if _momentum_candles else 'N'}"
                        )

                    # ── PRIORITY 5a: RETRACEMENT FORMING (fishing line entry) ──
                    # From chart study: the highest-probability entry is AT the E55/E100
                    # level with a reversal candle + stoch crossing back, BEFORE BB re-expands.
                    # This is Tim's primary setup. BB may still be contracting — expected.
                    # Fires before Priority 5b because this IS the entry point, not the confirmation.
                    elif _is_retracement_forming and not has_opportunity:
                        has_opportunity = True
                        alert_type = 'CRITERIA_MET'
                        _forming_label = _retracement_type.replace('_forming', '').upper()
                        _forming_signals = []
                        if _stoch_bull_cross or _stoch_bear_cross:
                            _forming_signals.append(f"stoch_cross(K={_stoch_k_now:.0f}/D={_stoch_d_now:.0f})")
                        if _rsi_bull_divergence or _rsi_bear_divergence:
                            _forming_signals.append("RSI_divergence")
                        if _reversal_candle_at_ema:
                            _forming_signals.append(f"reversal_candle@{_reversal_candle_ema_level}")
                        logger.info(
                            f"🎣 [{pair}] RETRACEMENT FORMING at {_forming_label}: "
                            f"fan={fan_direction} ordered | "
                            f"E55={_e55_dist_pips:.1f}p E100={_e100_dist_pips:.1f}p | "
                            f"Signals: {', '.join(_forming_signals)} | "
                            f"BB={'contracting(expected)' if not _bb_expanding else 'expanding'} | "
                            f"[FISHING LINE ENTRY — validator confirms]"
                        )

                    # ── PRIORITY 5b: RETRACEMENT CONFIRMED (BB re-expansion) ──
                    # Price tested E55/E100, BBs now re-expanding for 3+ bars.
                    # This confirms the move has already resumed (guardian-proven).
                    elif _is_retracement and not has_opportunity:
                        has_opportunity = True
                        alert_type = 'CRITERIA_MET'
                        _retrace_label = 'E55 SHALLOW' if _retracement_type == 'e55_shallow' else 'E100 DEEP'
                        logger.info(
                            f"\U0001f501 [{pair}] {_retrace_label} CONTINUATION: peak fan={_peak_fan_width:.3f}\u2192now={_fan_width_now:.3f} | "
                            f"Re-expansion confirmed ({_reexpansion_count} bars) | "
                            f"E55={_e55_dist_pips:.1f}p E100={_e100_dist_pips:.1f}p | "
                            f"BB re-expanding={'Y' if _bb_re_expanding else 'N'} | "
                            f"[TREND CONTINUATION ENTRY]"
                        )

                    # ── PRIORITY 6: REGIME CHANGE BREWING ──
                    # RSI extreme + BB constricting + fan peaked = retracement/regime change incoming
                    elif not has_opportunity:
                        _regime_rsi_extreme = _rsi_now > 72 or _rsi_now < 28
                        _regime_bb_tight = _bb_delta_5bar < -0.0002
                        _regime_fan_peaked = fan_state in ('peaked', 'decelerating', 'contracting')
                        _regime_change_brewing = _regime_rsi_extreme and _regime_bb_tight and _regime_fan_peaked
                        if _regime_change_brewing:
                            has_opportunity = True
                            alert_type = 'EARLY_WARNING'
                            _regime_dir = 'BEARISH reversal likely' if _rsi_now > 72 else 'BULLISH reversal likely'
                            logger.info(
                                f"🔮 [{pair}] REGIME CHANGE BREWING: RSI={_rsi_now:.0f} | "
                                f"BB constricting (Δ5={_bb_delta_5bar:+.5f}) | Fan {fan_state} | "
                                f"{_regime_dir} — watch for EMA crosses"
                            )

                    # ── PRIORITY 7: PARTIAL THESIS (fan expanding, checklist half-met) ──
                    elif _checklist_score >= 5 and _fan_expanding and not has_opportunity:
                        has_opportunity = True
                        alert_type = 'EARLY_WARNING'
                        logger.info(
                            f"\u26a0\ufe0f [{pair}] PARTIAL THESIS: checklist {_checklist_score}/10 + fan expanding | "
                            f"Missing: {[k for k,v in _checklist.items() if not v]}"
                        )

                    # ── PRIORITY 7: STORY SCORE — market narrative sees opportunity ──
                    elif not has_opportunity:
                        _story_entry = story.get('entry_type', 'none')
                        _story_score = story.get('opportunity_score', 0)
                        if _story_entry not in ('none', '') and _story_score >= 50:
                            has_opportunity = True
                            alert_type = 'EARLY_WARNING'
                            logger.info(
                                f"📖 [{pair}] STORY SIGNAL: {_story_entry} score={_story_score}/100 | "
                                f"Fan={fan_direction} {fan_state} | "
                                f"Thesis: {story.get('thesis', '')[:80]}"
                            )

                if has_opportunity:
                    # ── Gather context data for validator (NO direction decisions) ──
                    session_quality = get_session_quality(pair)
                    is_prime = is_prime_time(pair)
                    current_rsi = float(latest_row.get('RSI', latest_row.get('rsi', 50.0)))
                    current_stoch_k = float(latest_row.get('stoch_k', 50.0))
                    current_bb_position = self._get_bb_position(latest_row)
                    current_session_list = get_active_sessions()
                    current_session = current_session_list[0] if current_session_list else "off_hours"
                    current_regime = self._get_current_regime(latest_row)
                    current_candle_pattern = self._get_latest_candle_pattern(df)

                    # ── Real trade pattern matching + Setup Revenue grading ──
                    # Two data sources:
                    #   1. manual_trades: individual trade fingerprint matching (thesis similarity)
                    #   2. setup_revenue: aggregated lifetime P&L per setup+pair (gross revenue, WR, frequency)
                    # Scout grades opportunities HIGHER when the same setup has won before,
                    # especially if it's made money across multiple trades and/or users.
                    trade_evidence = {
                        'matching_wins': 0, 'matching_losses': 0,
                        'total_wins': 0, 'total_losses': 0,
                        'pair_wins': 0, 'pair_losses': 0,
                        'avg_win_pips': 0, 'thesis_match_wr': None,
                        'similar_trades': [],
                        # Setup revenue data (from setup_revenue table)
                        'setup_revenue': [],         # top setups for this pair by gross revenue
                        'pair_gross_revenue': 0.0,   # total lifetime $ made on this pair
                        'pair_total_trades': 0,      # how many times this pair has been traded
                        'pair_best_setup': None,     # highest-revenue setup for this pair
                        'pair_best_setup_wr': 0.0,   # WR of the best setup
                        'pair_best_setup_revenue': 0.0,  # $ of the best setup
                        'cross_pair_winners': [],        # setups winning on 2+ pairs
                    }
                    try:
                        _mt_conn = get_trading_forex()

                        # Overall stats from live_trades (unified trade table)
                        _stats = _mt_conn.execute("""
                            SELECT result, COUNT(*), AVG(pips)
                            FROM live_trades WHERE result IN ('win','loss')
                            GROUP BY result
                        """).fetchall()
                        for _r in _stats:
                            if _r[0] == 'win':
                                trade_evidence['total_wins'] = _r[1]
                                trade_evidence['avg_win_pips'] = round(_r[2] or 0, 1)
                            else:
                                trade_evidence['total_losses'] = _r[1]

                        # Pair-specific stats from live_trades (unified)
                        _pair_stats = _mt_conn.execute("""
                            SELECT result, COUNT(*) FROM live_trades
                            WHERE pair=? AND result IN ('win','loss') GROUP BY result
                        """, (pair,)).fetchall()
                        for _r in _pair_stats:
                            if _r[0] == 'win': trade_evidence['pair_wins'] = _r[1]
                            else: trade_evidence['pair_losses'] = _r[1]

                        # ── SETUP REVENUE: aggregated lifetime performance per setup+pair ──
                        # This is the core feedback loop: winning setups get graded by
                        # gross revenue ($), frequency (trade count), and win rate.
                        try:
                            _rev_rows = _mt_conn.execute("""
                                SELECT setup_name, pair, total_trades, wins, losses,
                                       total_pips, total_usd, win_rate, promoted
                                FROM setup_revenue
                                WHERE pair = ? AND total_trades >= 1
                                ORDER BY total_usd DESC
                            """, (pair,)).fetchall()
                            for _rev in _rev_rows:
                                _rev_entry = {
                                    'setup_name': _rev[0],
                                    'pair': _rev[1],
                                    'total_trades': _rev[2],
                                    'wins': _rev[3],
                                    'losses': _rev[4],
                                    'total_pips': round(float(_rev[5] or 0), 1),
                                    'gross_revenue': round(float(_rev[6] or 0), 2),
                                    'win_rate': round(float(_rev[7] or 0) * 100, 1),
                                    'promoted': bool(_rev[8]),
                                }
                                trade_evidence['setup_revenue'].append(_rev_entry)
                                trade_evidence['pair_total_trades'] += _rev_entry['total_trades']
                                trade_evidence['pair_gross_revenue'] += _rev_entry['gross_revenue']

                            # Identify the best setup for this pair (by gross revenue)
                            if trade_evidence['setup_revenue']:
                                _best = trade_evidence['setup_revenue'][0]  # already sorted by total_usd DESC
                                if _best['gross_revenue'] > 0:
                                    trade_evidence['pair_best_setup'] = _best['setup_name']
                                    trade_evidence['pair_best_setup_wr'] = _best['win_rate']
                                    trade_evidence['pair_best_setup_revenue'] = _best['gross_revenue']
                        except Exception as _rev_err:
                            logger.debug("Setup revenue lookup failed: %s", _rev_err)

                        # ── ALL setups across all pairs (for cross-pair pattern grading) ──
                        # If a setup has won on multiple pairs, it's a stronger signal
                        try:
                            _cross_pair = _mt_conn.execute("""
                                SELECT setup_name, COUNT(DISTINCT pair) as pairs_traded,
                                       SUM(total_trades) as total_trades,
                                       SUM(wins) as total_wins,
                                       SUM(total_usd) as gross_revenue
                                FROM setup_revenue
                                WHERE wins >= 1
                                GROUP BY setup_name
                                HAVING pairs_traded >= 2
                                ORDER BY gross_revenue DESC
                                LIMIT 5
                            """).fetchall()
                            trade_evidence['cross_pair_winners'] = [
                                {
                                    'setup_name': _cp[0],
                                    'pairs_traded': _cp[1],
                                    'total_trades': _cp[2],
                                    'total_wins': _cp[3],
                                    'gross_revenue': round(float(_cp[4] or 0), 2),
                                }
                                for _cp in _cross_pair
                            ]
                        except Exception:
                            trade_evidence['cross_pair_winners'] = []

                        # Thesis pattern matching: compare current conditions to each past trade
                        _all_trades = _mt_conn.execute("""
                            SELECT pair, direction, result, pips, fan_state, bb_expanding,
                                   rsi, fan_width_pct, pattern_fingerprint, hold_bars,
                                   classified_setup
                            FROM live_trades WHERE result IN ('win','loss')
                        """).fetchall()

                        for _t in _all_trades:
                            _t_pair, _t_dir, _t_result, _t_pips, _t_fan, _t_bb, _t_rsi, _t_fw, _t_fp, _t_bars, _t_setup = _t

                            # Match thesis conditions (not pair or direction — thesis is universal)
                            _match_score = 0
                            _t_bb_val = str(_t_bb).lower() in ('true', '1', 'yes', 'expanding')
                            _t_fan_exp = str(_t_fan).lower() in ('expanding', 'accelerating')

                            if _fan_expanding == _t_fan_exp: _match_score += 1
                            if _bb_expanding == _t_bb_val: _match_score += 1
                            if _t_rsi:
                                _t_rsi_healthy = 25 < float(_t_rsi) < 75
                                if _rsi_healthy == _t_rsi_healthy: _match_score += 1
                            if _t_fp:
                                _fp_has_bb_exp = 'bb_exp' in str(_t_fp)
                                if _bb_expanding == _fp_has_bb_exp: _match_score += 1
                            # Bonus: same pair = stronger match
                            if _t_pair == pair: _match_score += 1

                            # 3+ matches out of 5 = similar setup
                            if _match_score >= 3:
                                if _t_result == 'win':
                                    trade_evidence['matching_wins'] += 1
                                else:
                                    trade_evidence['matching_losses'] += 1
                                trade_evidence['similar_trades'].append({
                                    'pair': _t_pair, 'direction': _t_dir,
                                    'result': _t_result, 'pips': round(float(_t_pips or 0), 1),
                                    'fingerprint': _t_fp,
                                    'setup_name': _t_setup,
                                    'match_score': _match_score,
                                })

                        _total_matching = trade_evidence['matching_wins'] + trade_evidence['matching_losses']
                        if _total_matching >= 2:
                            trade_evidence['thesis_match_wr'] = round(
                                (trade_evidence['matching_wins'] / _total_matching) * 100, 1
                            )
                    except Exception as _te:
                        logger.debug("Trade evidence lookup failed: %s", _te)

                    # Legacy compat
                    playbook_context = {}
                    live_history = trade_evidence

                    # ── Classified setups (as context, not direction decisions) ──
                    classified_context = []
                    if classified_setups:
                        for cs in classified_setups[:5]:
                            classified_context.append({
                                'setup': cs['setup'],
                                'name': cs['name'],
                                'direction': cs['direction'],
                                'confidence': cs['confidence'],
                                'regime_valid': cs.get('regime_valid', False),
                            })

                    # ── Build V4 alert (NO direction) ──
                    alert = {
                        'timestamp': datetime.now().isoformat(),
                        'pair': pair,
                        'alert_type': alert_type,
                        'direction': None,  # V4: scout NEVER sets direction

                        # Checklist (mirrors validator's 10-point system)
                        'checklist': _checklist,
                        'checklist_score': _checklist_score,

                        # Sniper data (retracement/reversal signal, NOT a gate)
                        'sniper_triggered': sniper_triggered,
                        'v4_score': max_score,
                        'v4_bull_score': bull_score,
                        'v4_bear_score': bear_score,

                        # Thesis conditions (V4 real measurements)
                        'fan_state': fan_state,
                        'fan_direction': fan_direction,
                        'fan_width_pct': round(_fan_width_now, 4),
                        'fan_delta_5bar': round(_fan_delta_5bar, 5),
                        'fan_delta_20bar': round(_fan_delta_20bar, 5),
                        'fan_expanding': _fan_expanding,
                        'fan_accelerating': _fan_accelerating,
                        'bb_expanding': _bb_expanding,
                        'bb_delta_5bar': round(_bb_delta_5bar, 5),
                        'bb_delta_20bar': round(_bb_delta_20bar, 5),
                        'bb_width': round(_bb_width_now, 5),
                        'candles_moving_away': _candles_moving_away,
                        'recent_cross': _recent_cross,
                        'cross_bars_ago': _cross_bars_ago,
                        'cross1_direction': _cross1_direction,
                        'cross2_detected': _cross2_detected,
                        'cross2_bars_ago': _cross2_bars_ago,
                        'cross2_direction': _cross2_direction,
                        'dual_cross_cascade': _dual_cross_cascade,
                        'cascade_direction': _cascade_direction,
                        'e55_distance_pips': round(_e55_dist_pips, 1),
                        'e100_distance_pips': round(_e100_dist_pips, 1),
                        'momentum_candles': _momentum_candles,
                        'candles_correct_side': _candles_correct_side,

                        # RSI state
                        'rsi_at_alert': round(_rsi_now, 1),
                        'rsi_was_extreme': _rsi_was_extreme,
                        'rsi_extreme_value': round(_rsi_extreme_val, 1) if _rsi_extreme_val else None,
                        'rsi_recovery_ok': _rsi_recovery_ok,
                        'rsi_healthy': _rsi_healthy,

                        # Retracement data
                        'is_retracement': _is_retracement,
                        'retracement_type': _retracement_type,
                        'was_expanding_recently': _was_expanding_recently,
                        'peak_fan_width': round(_peak_fan_width, 4),
                        'candles_holding_above_emas': _candles_holding,
                        'bb_re_expanding': _bb_re_expanding,
                        'tested_e55': _tested_e55,
                        'tested_e100': _tested_e100,

                        # Market story
                        'story_score': story.get('opportunity_score', 0),
                        'story_thesis': story.get('thesis', ''),
                        'story_entry_type': story.get('entry_type', 'none'),
                        'story_narrative': story.get('narrative', ''),
                        'story_confidence': story.get('confidence', 0),
                        'story_warnings': story.get('warnings', []),

                        # Current indicators
                        'current_rsi': current_rsi,
                        'current_stoch_k': current_stoch_k,
                        'current_stoch_d': float(latest_row.get('stoch_d', 50)),
                        'bb_position': current_bb_position,
                        'candle_pattern': current_candle_pattern,
                        'atr': _atr_val,
                        'h4_bias': latest_row.get('h4_bias', 'unknown'),

                        # Session context
                        'session_quality': session_quality,
                        'is_prime_time': is_prime,
                        'active_sessions': current_session_list,
                        'regime': current_regime,

                        # Historical context for validator
                        'playbook_context': playbook_context,
                        'live_history': live_history,
                        'classified_setups': classified_context,

                        # EMA data
                        'ema_data': ema_signal,

                        # Full market snapshot
                        'market_snapshot': {
                            'price': float(latest_row.get('close', 0)),
                            'rsi': current_rsi,
                            'stoch_k': current_stoch_k,
                            'stoch_d': float(latest_row.get('stoch_d', 50)),
                            'atr': float(latest_row.get('atr', 0)),
                            'adx': float(latest_row.get('adx', 0)),
                            'bb_upper': float(latest_row.get('bb_upper', 0)),
                            'bb_lower': float(latest_row.get('bb_lower', 0)),
                            'bb_middle': float(latest_row.get('bb_middle', 0)),
                            'fan_state': ema_signal.get('fan_state', 'unknown'),
                            'fan_direction': ema_signal.get('fan_direction', 'neutral'),
                            'separation_pct': ema_signal.get('separation_pct', 0),
                            'separation_velocity': ema_signal.get('separation_velocity', 0),
                            'trend_health': ema_signal.get('trend_health', 0),
                            'reversal_risk': ema_signal.get('reversal_risk'),
                            'v4_bull': bull_score,
                            'v4_bear': bear_score,
                            'regime': current_regime,
                            'session': current_session,
                        },

                        # Backward compat
                        'setup_name': f"V4_{alert_type}",
                        'score': story.get('opportunity_score', 0),
                        'reasoning': story.get('thesis', ''),

                        # Setup ID — derived from best classified setup (required by Gate 5a)
                        'setup_id': classified_best[0]['setup'] if classified_best else f"V4_{alert_type}",
                    }
                    # 2026-05-10: inject (setup, pair) lifetime track record so the
                    # validator/TA prompt sees historical value. For V4 alerts we look
                    # up by setup_id (the classified S/C setup) when available, else by
                    # the V4_* setup_name. See trading_cycle.py:1834.
                    _v4_lookup_key = alert.get('setup_id') or alert.get('setup_name')
                    alert.update(self._lookup_setup_track_record(_v4_lookup_key, pair))

                    # ── Quality filter: EARLY_WARNING with low score → skip ──
                    # ── QUALITY GATE: EARLY_WARNING composite filter ──────────────
                    # Old filter: just story_score < 30 (caught almost nothing)
                    # New filter: must have EITHER fan expanding OR retracement setup
                    # AND BB width must be meaningful (not just a 1-bar flicker)
                    # Weak pairs require CRITERIA_MET (not just EARLY_WARNING)
                    if alert_type == 'EARLY_WARNING' and pair in _WEAK_PAIRS and pair not in _snipe_list_pairs:
                        logger.info(
                            f"\U0001f6ab [{pair}] EARLY_WARNING blocked for weak pair "
                            f"(0-33%WR from training data) — needs CRITERIA_MET signal"
                        )
                        self._pair_cooldowns[pair] = time.time() + self.scan_interval
                        return []

                    if alert_type == 'EARLY_WARNING':
                        _ew_story = story.get('opportunity_score', 0)
                        _ew_bb_meaningful = _bb_width_now > 0.0003  # BB must have real width
                        _ew_has_structure = _fan_expanding or _is_retracement or _bb_squeeze_breakout
                        _ew_min_story = 20 if pair in _snipe_list_pairs else 35
                        _ew_ok = _ew_story >= _ew_min_story and _ew_bb_meaningful and _ew_has_structure
                        if not _ew_ok:
                            logger.info(
                                f"\U0001f6ab [{pair}] V4 FILTERED EARLY_WARNING: story={_ew_story} "
                                f"bb_width={_bb_width_now:.4f}(meaningful={_ew_bb_meaningful}) "
                                f"has_structure={_ew_has_structure} "
                                f"(need story>=35 + real BB + fan/retrace structure)"
                            )
                            self._pair_cooldowns[pair] = time.time() + self.scan_interval
                            return []

                    # ── Direction filter: mixed/neutral → no actionable trade ──
                    _alert_dir = fan_direction if isinstance(fan_direction, str) else ema_signal.get('fan_direction', 'mixed')
                    if _alert_dir in ('mixed', 'neutral', ''):
                        logger.info(f"\U0001f6ab [{pair}] V4 FILTERED: fan_direction={_alert_dir} — no clear direction, skipping cycle")
                        self._pair_cooldowns[pair] = time.time() + self.scan_interval
                        return []

                    # ── S15 COUNTER-TREND GUARDRAILS ──
                    # S15 setups lost -222 pips (avg -17.6p per trade, blowouts to -51.9p).
                    # S15 is ONLY valid at TRUE EXHAUSTION: fan peaked/decelerating + RSI extreme.
                    # During consolidation or ranging, S15 fires into chop = guaranteed loss.
                    # Backtest evidence: S15 exhaustion sells at RSI 80-100 = 98.2% WR.
                    _has_s15_setup = False
                    if classified_setups:
                        for _cs in classified_setups:
                            _cs_name = str(_cs.get('name', '')).lower()
                            _cs_setup = str(_cs.get('setup', '')).lower()
                            if 's15' in _cs_name or 's15' in _cs_setup or 'counter' in _cs_name:
                                _has_s15_setup = True
                                break

                    if _has_s15_setup and not _is_retracement:
                        # S15 is only valid at exhaustion: fan peaked + RSI at extreme
                        _s15_fan_peaked = fan_state in ('peaked', 'decelerating')
                        _s15_rsi_extreme = _rsi_now > 75 or _rsi_now < 25  # true overbought/oversold
                        _s15_prior_expansion = _was_expanding_recently and _peak_fan_width > 0.05
                        _s15_valid = _s15_fan_peaked and _s15_rsi_extreme and _s15_prior_expansion

                        if not _s15_valid:
                            _s15_reason = []
                            if not _s15_fan_peaked:
                                _s15_reason.append(f"fan={fan_state}(need peaked/decel)")
                            if not _s15_rsi_extreme:
                                _s15_reason.append(f"RSI={_rsi_now:.0f}(need <25 or >75)")
                            if not _s15_prior_expansion:
                                _s15_reason.append(f"no prior expansion(peak={_peak_fan_width:.3f})")
                            logger.info(
                                f"\U0001f6ab [{pair}] S15 COUNTER-TREND BLOCKED: not at true exhaustion | "
                                f"{', '.join(_s15_reason)} | S15 only valid at peaked fan + RSI extreme"
                            )
                            try:
                                flight.record(FlightStage.SCOUT_SCAN, pair=pair, data={
                                    'substage': 's15_blocked',
                                    'fan_state': fan_state,
                                    'rsi': round(_rsi_now, 1),
                                    'fan_peaked': _s15_fan_peaked,
                                    'rsi_extreme': _s15_rsi_extreme,
                                    'prior_expansion': _s15_prior_expansion,
                                    'reason': ', '.join(_s15_reason),
                                }, note=f"S15 BLOCKED: {', '.join(_s15_reason)}")
                            except Exception:
                                pass
                            self._pair_cooldowns[pair] = time.time() + self.scan_interval
                            return []

                    # ── Rejection cooldown ──
                    _reject_key = f"{pair}::V4"
                    if not hasattr(self, '_rejection_cooldowns'):
                        self._rejection_cooldowns = {}
                    try:
                        _rej_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                 'dashboard', 'rejection_cooldowns.json')
                        if os.path.exists(_rej_path):
                            with open(_rej_path) as _rf:
                                self._rejection_cooldowns = json.load(_rf)
                    except Exception:
                        pass
                    _reject_until = self._rejection_cooldowns.get(_reject_key, 0)
                    if time.time() < _reject_until:
                        _mins_left = (_reject_until - time.time()) / 60
                        logger.info(f"\U0001f6ab [{pair}] V4 FILTERED: rejected recently, {_mins_left:.0f}min cooldown")
                        self._pair_cooldowns[pair] = time.time() + self.scan_interval
                        return []

                    alerts.append(alert)
                    _finding_id = self._record_scout_finding(alert, ema_signal, mkt_picture, session_quality)
                    # Attach finding_id so downstream can link snipe → finding
                    if _finding_id:
                        alert["finding_id"] = _finding_id

                    # ── Queue V4 pipeline ──
                    self._queue_scout_cycle(pair, alert)
                    self._pair_cooldowns[pair] = time.time() + self.scan_interval
                    self._save_cooldowns()  # persist so restarts don't re-fire immediately

                    logger.info(
                        f"\U0001f4e1 V4 ALERT: {pair} {alert_type} | "
                        f"v4={max_score} | Fan: {fan_direction} {fan_state} | "
                        f"BB exp={_bb_expanding} | E100={_e100_dist_pips:.1f}p | "
                        f"Story: {story.get('opportunity_score')}/100"
                    )

                    # ── Flight: alert generated ──
                    flight.record(FlightStage.SCOUT_ALERT, pair=pair, data={
                        "pair": pair, "alert_type": alert_type,
                        "direction": fan_direction,
                        "entry_type": story.get('entry_type', 'none'),
                        "opportunity_score": story.get('opportunity_score', 0),
                        "v4_score": max_score,
                        "fan_state": fan_state,
                        "bb_expanding": _bb_expanding,
                        "story_score": story.get('opportunity_score', 0),
                    }, duration_ms=(time.time() - _scan_start) * 1000,
                    note=f"V4 {alert_type} v4={max_score}")

                else:
                    # No opportunity
                    logger.debug(
                        f"[{pair}] No V4 opportunity | Fan: {ema_signal.get('fan_direction','?')} "
                        f"{ema_signal.get('fan_state','?')} | v4: {max_score} | "
                        f"Score: {story['opportunity_score']}/100"
                    )

                # ── Flight: scan complete ──
                flight.record(FlightStage.SCOUT_SCAN, pair=pair, data={
                    "story_score": story['opportunity_score'],
                    "entry_type": story.get('entry_type', 'none'),
                    "fan_state": ema_signal.get('fan_state', 'unknown'),
                    "fan_direction": ema_signal.get('fan_direction', 'unknown'),
                    "v4_buy": bull_score, "v4_sell": bear_score,
                    "has_opportunity": has_opportunity,
                    "alert_type": alert_type,
                }, duration_ms=(time.time() - _scan_start) * 1000,
                note=f"V4 {'ALERT ' + str(alert_type) if alerts else 'no opportunity'}")

                # Store market picture for dashboard access
                self._latest_market_pictures[pair] = mkt_picture

                # ── TIER 1 SETUP DETECTORS (additive triggers alongside V4) ──
                # Validated by 90d × 14-pair × 8-fold walk-forward backtest.
                # All 7 detectors: WR>=80%, sd_WR<=5pp, zero negative folds,
                # 92-100% NEW signal vs V4 (overlap analysis 2026-04-29).
                # Each fires independently; existing scout cooldown + watch
                # dedup handles overlap. Per-detector cooldown also applied.
                # IMPORTANT: This block runs REGARDLESS of V4 has_opportunity,
                # so it computes its own context vars (V4-side vars only exist
                # inside the `if has_opportunity:` branch).
                try:
                    from scout_setup_detectors import run_tier1_detectors
                    if not hasattr(self, "_tier1_cooldowns"):
                        self._tier1_cooldowns = {}  # (pair, detector) -> ts
                    tier1_fires = run_tier1_detectors(df)
                    if tier1_fires:
                        # Compute context vars locally — these may not be in scope
                        # if V4 said no opportunity (they're set inside that branch).
                        _t1_session_quality = get_session_quality(pair)
                        _t1_is_prime = is_prime_time(pair)
                        _t1_current_rsi = float(latest_row.get('RSI', latest_row.get('rsi', 50.0)))
                        _t1_stoch_k = float(latest_row.get('stoch_k', 50.0))
                        _t1_stoch_d = float(latest_row.get('stoch_d', 50.0))
                        _t1_bb_pos = self._get_bb_position(latest_row)
                        _t1_session_list = get_active_sessions()
                        _t1_session = _t1_session_list[0] if _t1_session_list else "off_hours"
                        _t1_regime = self._get_current_regime(latest_row)
                        _t1_candle_pat = self._get_latest_candle_pattern(df)
                        _t1_atr = float(latest_row.get('atr', 0))
                    for det_name, det_dir in tier1_fires:
                        cooldown_key = (pair, det_name)
                        last_fire = self._tier1_cooldowns.get(cooldown_key, 0)
                        if time.time() - last_fire < 1800:  # 30-min per-detector cooldown
                            continue
                        # Build alert mirroring V4 shape but with detector-set direction
                        t1_alert = {
                            'timestamp': datetime.now().isoformat(),
                            'pair': pair,
                            'alert_type': det_name,
                            'direction': det_dir.upper(),  # 'BUY' or 'SELL'
                            'fan_state': fan_state,
                            'fan_direction': fan_direction,
                            'fan_width_pct': round(_fan_width_now, 4),
                            'fan_delta_5bar': round(_fan_delta_5bar, 5),
                            'fan_delta_20bar': round(_fan_delta_20bar, 5),
                            'fan_expanding': _fan_expanding,
                            'fan_accelerating': _fan_accelerating,
                            'bb_expanding': _bb_expanding,
                            'bb_delta_5bar': round(_bb_delta_5bar, 5),
                            'bb_delta_20bar': round(_bb_delta_20bar, 5),
                            'bb_width': round(_bb_width_now, 5),
                            'candles_moving_away': _candles_moving_away,
                            'recent_cross': _recent_cross,
                            'is_retracement': _is_retracement,
                            'retracement_type': _retracement_type,
                            'bb_re_expanding': _bb_re_expanding,
                            'tested_e55': _tested_e55,
                            'tested_e100': _tested_e100,
                            'story_score': story.get('opportunity_score', 0),
                            'story_thesis': story.get('thesis', ''),
                            'story_entry_type': story.get('entry_type', 'tier1_detector'),
                            'current_rsi': _t1_current_rsi,
                            'current_stoch_k': _t1_stoch_k,
                            'current_stoch_d': _t1_stoch_d,
                            'bb_position': _t1_bb_pos,
                            'candle_pattern': _t1_candle_pat,
                            'atr': _t1_atr,
                            'h4_bias': latest_row.get('h4_bias', 'unknown'),
                            'session_quality': _t1_session_quality,
                            'is_prime_time': _t1_is_prime,
                            'active_sessions': _t1_session_list,
                            'regime': _t1_regime,
                            'playbook_context': playbook_context if 'playbook_context' in dir() else [],
                            'live_history': live_history if 'live_history' in dir() else {},
                            'classified_setups': classified_context if 'classified_context' in dir() else [],
                            'ema_data': ema_signal,
                            'market_snapshot': {
                                'price': float(latest_row.get('close', 0)),
                                'rsi': _t1_current_rsi,
                                'stoch_k': _t1_stoch_k,
                                'stoch_d': _t1_stoch_d,
                                'atr': _t1_atr,
                                'adx': float(latest_row.get('adx', 0)),
                                'bb_upper': float(latest_row.get('bb_upper', 0)),
                                'bb_lower': float(latest_row.get('bb_lower', 0)),
                                'bb_middle': float(latest_row.get('bb_middle', 0)),
                                'fan_state': ema_signal.get('fan_state', 'unknown'),
                                'fan_direction': ema_signal.get('fan_direction', 'neutral'),
                                'separation_pct': ema_signal.get('separation_pct', 0),
                                'separation_velocity': ema_signal.get('separation_velocity', 0),
                                'trend_health': ema_signal.get('trend_health', 0),
                                'reversal_risk': ema_signal.get('reversal_risk'),
                                'regime': _t1_regime,
                                'session': _t1_session,
                            },
                            'setup_name': det_name,
                            'setup_id': det_name,
                            'score': story.get('opportunity_score', 0),
                            'reasoning': f"Tier 1 detector {det_name} fired {det_dir.upper()}",
                        }
                        # 2026-05-10: inject (setup, pair) lifetime track record so the
                        # validator/TA prompt at trading_cycle.py:1834 sees historical
                        # value (WR, trade count, gross USD/pips, promoted status).
                        t1_alert.update(self._lookup_setup_track_record(det_name, pair))
                        alerts.append(t1_alert)
                        _t1_finding_id = self._record_scout_finding(t1_alert, ema_signal, mkt_picture, _t1_session_quality)
                        if _t1_finding_id:
                            t1_alert["finding_id"] = _t1_finding_id
                        self._queue_scout_cycle(pair, t1_alert)
                        self._tier1_cooldowns[cooldown_key] = time.time()
                        logger.info(
                            f"\U0001f4e1 TIER1 ALERT: {pair} {det_name} {det_dir.upper()} | "
                            f"Fan: {fan_direction} {fan_state} | RSI={_t1_current_rsi:.0f} "
                            f"Stoch={_t1_stoch_k:.0f} ADX={float(latest_row.get('adx', 0)):.0f}"
                        )
                        flight.record(FlightStage.SCOUT_ALERT, pair=pair, data={
                            "pair": pair, "alert_type": det_name,
                            "direction": det_dir.upper(),
                            "fan_state": fan_state,
                            "fan_direction": fan_direction,
                            "rsi": _t1_current_rsi,
                            "stoch_k": _t1_stoch_k,
                            "tier1": True,
                        }, note=f"Tier1 {det_name} {det_dir.upper()}")
                except Exception as t1_exc:
                    logger.warning("Tier 1 detectors failed for %s: %s", pair, t1_exc)
                    import traceback; logger.debug("Tier 1 traceback: %s", traceback.format_exc())

                # ── CHECK ACTIVE SNIPES (uses Scout's already-computed data) ──
                try:
                    snipe_triggers = self._check_snipes_for_pair(
                        pair=pair,
                        bull_score=bull_score,
                        bear_score=bear_score,
                        indicators=latest_row,
                        market_picture=mkt_picture,
                        market_snapshot=alerts[0].get('market_snapshot') if alerts else None,
                        market_story=story,
                        alert_type=alert_type,
                    )
                    for snipe in snipe_triggers:
                        await self._broadcast_alert(snipe)
                    if snipe_triggers:
                        flight.record(FlightStage.SCOUT_SNIPE_CHECK, pair=pair, data={
                            "triggered": len(snipe_triggers),
                            "watch_ids": [s.get("watch_id") for s in snipe_triggers],
                        }, note=f"{len(snipe_triggers)} snipes triggered")
                except Exception as snipe_exc:
                    logger.warning("Snipe check failed for %s: %s", pair, snipe_exc)
                    flight.record(FlightStage.SCOUT_SNIPE_CHECK, pair=pair,
                                  status="error", note=str(snipe_exc)[:200])

                # Store and broadcast alerts
                for alert in alerts:
                    self._store_alert(alert)
                    await self._broadcast_alert(alert)

                return alerts

        except Exception as e:
            import traceback
            logger.error(f"Error scanning {pair}: {e}\n{traceback.format_exc()}")
            return []

    def _candles_to_dataframe(self, candles: List[Dict]) -> pd.DataFrame:
        """Convert OANDA candles to DataFrame."""
        data = []
        for candle in candles:
            if candle['complete']:
                data.append({
                    'time': candle['time'],
                    'open': float(candle['mid']['o']),
                    'high': float(candle['mid']['h']),
                    'low': float(candle['mid']['l']),
                    'close': float(candle['mid']['c']),
                    'volume': int(candle.get('volume', 0))
                })

        df = pd.DataFrame(data)
        df['time'] = pd.to_datetime(df['time'])
        df.set_index('time', inplace=True)
        return df

    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate all technical indicators."""
        # Trend indicators
        df['ema_21'] = ema(df, 21)
        df['ema_55'] = ema(df, 55)
        df['ema_100'] = ema(df, 100)
        df['sma_200'] = sma(df, 200)

        # Momentum indicators
        df['rsi'] = rsi(df, 14)
        stoch_data = stochastic(df, 14, 3)
        df['stoch_k'] = stoch_data['stoch_k']
        df['stoch_d'] = stoch_data['stoch_d']

        # Volatility indicators
        bb_data = bollinger_bands(df, 20, 2)
        df['bb_upper'] = bb_data['bb_upper']
        df['bb_middle'] = bb_data['bb_middle']
        df['bb_lower'] = bb_data['bb_lower']
        df['atr'] = atr(df, 14)

        # Trend strength
        adx_data = adx(df, 14)
        df['adx'] = adx_data['adx']
        df['plus_di'] = adx_data['plus_di']
        df['minus_di'] = adx_data['minus_di']

        # MACD
        macd_data = macd(df, 12, 26, 9)
        df['macd'] = macd_data['macd_line']
        df['macd_signal'] = macd_data['macd_signal']
        df['macd_histogram'] = macd_data['macd_histogram']

        # Parabolic SAR
        df['sar'] = parabolic_sar(df)

        # Calculate additional derived indicators needed for sniper v4
        df['bb_lower_pen'] = (df['bb_lower'] - df['close']) / df['atr']
        df['bb_upper_pen'] = (df['close'] - df['bb_upper']) / df['atr']
        df['rsi_slope'] = df['rsi'].diff()

        return df

    def _dataframe_to_candles(self, df: pd.DataFrame) -> List[Dict]:
        """Convert DataFrame back to candles format for EMA analysis."""
        candles = []
        for idx, row in df.iterrows():
            candles.append({
                'time': idx.isoformat(),
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close'])
            })
        return candles

    def _create_ema_alert(self, pair: str, ema_signal: Dict, latest_row: pd.Series) -> Dict:
        """Create an EMA separation alert with velocity and elite boost data."""
        direction = ema_signal['signal'].upper()

        # Enhanced reasoning with velocity and elite boost zone
        velocity_class = ema_signal.get('velocity_class', 'unknown')
        elite_boost = ema_signal.get('elite_boost', False)
        candles_since_cross = ema_signal.get('candles_since_cross', 999)

        reasoning = (
            f"EMA {ema_signal['phase']}: {ema_signal['separation_pct']:.3f}% separation "
            f"({'ELITE BOOST' if elite_boost else 'standard'}) | "
            f"Velocity: {velocity_class} ({ema_signal.get('velocity', 0):.4f}) | "
            f"EMA100 {ema_signal['ema100_role']} | "
            f"Candles since cross: {candles_since_cross} | "
            f"RSI: {latest_row.get('rsi', 0):.1f}"
        )

        # Create the alert with full payload as specified in requirements
        alert = {
            'type': 'ema_separation',
            'pair': pair,
            'separation_pct': ema_signal['separation_pct'],
            'velocity': ema_signal.get('velocity', 0.0),
            'velocity_class': velocity_class,
            'ema100_role': ema_signal['ema100_role'],
            'phase': ema_signal['phase'],
            'candles_since_cross': candles_since_cross,
            'elite_boost': elite_boost,
            'direction': direction.lower(),

            # Standard alert fields for compatibility
            'timestamp': datetime.now().isoformat(),
            'setup_name': 'EMA_Separation',
            'score': ema_signal['strength'],
            'historical_win_rate': 75.0,  # Estimated based on strategy
            'historical_trade_count': 500,  # Estimated
            'historical_profit_factor': 1.8,  # Estimated
            'current_rsi': latest_row.get('rsi'),
            'current_stoch_k': latest_row.get('stoch_k'),
            'current_stoch_d': latest_row.get('stoch_d'),
            'bb_position': self._get_bb_position(latest_row),
            'candle_pattern': 'EMA Pattern',
            'h4_bias': latest_row.get('h4_bias', 'unknown'),
            'reasoning': reasoning,
            'ema_data': ema_signal  # Store full EMA data for dashboard
        }

        return alert

    def _create_market_picture_alert(self, pair: str, mkt_picture: Dict, reason: str, latest_row: pd.Series) -> Dict:
        """Create an alert from a full market picture with narrative context."""
        ema = mkt_picture.get('ema', {})
        rsi = mkt_picture.get('rsi', {})
        stoch = mkt_picture.get('stochastic', {})
        bb = mkt_picture.get('bollinger', {})

        direction = ema.get('signal', 'neutral')
        # For counter-trend setups, the trade direction is OPPOSITE the EMA direction
        if reason == 'HIGH_CONFLUENCE_REVERSAL':
            direction = 'sell' if ema.get('fan_direction') == 'bullish' else 'buy'
        elif reason == 'COUNTER_TREND_WINDOW':
            direction = 'sell' if ema.get('fan_direction') == 'bullish' else 'buy'

        # Normalize to BULL/BEAR/neutral (consistent with story alerts)
        if direction == 'buy':
            direction = 'BULL'
        elif direction == 'sell':
            direction = 'BEAR'
        # 'neutral' stays as-is

        alert = {
            'type': 'market_picture',
            'pair': pair,
            'reason': reason,
            'direction': direction,
            'timestamp': datetime.now().isoformat(),
            'setup_name': f'MKT_{reason}',
            'score': ema.get('trend_health', 0),

            # EMA context
            'fan_direction': ema.get('fan_direction', 'mixed'),
            'fan_state': ema.get('fan_state', 'unknown'),
            'separation_pct': ema.get('separation_pct', 0),
            'velocity': ema.get('separation_velocity', 0),
            'velocity_trend': ema.get('fan_velocity_trend', 'unknown'),
            'trend_health': ema.get('trend_health', 0),
            'reversal_risk': ema.get('reversal_risk', 'unknown'),
            'ema100_role': ema.get('ema100_role', 'neutral'),
            'e100_pattern': ema.get('e100_candle_pattern'),

            # Other indicators
            'rsi': rsi.get('value'),
            'rsi_zone': rsi.get('zone', 'neutral'),
            'stoch_k': stoch.get('k'),
            'stoch_zone': stoch.get('zone', 'neutral'),
            'bb_position': bb.get('position', 'neutral'),
            'bb_squeeze': bb.get('squeeze', False),

            # Narratives
            'ema_narrative': ema.get('narrative', ''),
            'confluence_narrative': mkt_picture.get('confluence_narrative', ''),
            'recommended_bias': mkt_picture.get('recommended_bias', 'neutral'),

            # Legacy compat
            'reasoning': mkt_picture.get('confluence_narrative', '')[:300],
            'ema_data': ema,

            # DB compat fields
            'historical_win_rate': 0.0,
            'historical_trade_count': 0,
            'historical_profit_factor': 0.0,
            'candle_pattern': '',
            'h4_bias': 'unknown',
        }

        return alert

    def _lookup_setup_track_record(self, setup_name: str, pair: str) -> dict:
        """Pull lifetime track record for (setup_name, pair) from setup_revenue.

        Used to enrich scout alerts with the historical value of the setup being
        fired — so when scout passes an alert to TA + validator, they see WR,
        trade count, gross USD, gross pips, profit factor, and promoted status.
        See validator/TA prompt at trading_cycle.py:1834 which formats these.

        Returns an empty-defaults dict if the (setup, pair) row doesn't exist yet.
        Added 2026-05-10 per Tim's request for setup value-context propagation.
        """
        defaults = {
            'win_rate': 0.0,
            'trade_count': 0,
            'wins': 0, 'losses': 0,
            'gross_revenue': 0.0,
            'gross_revenue_pips': 0.0,
            'profit_factor': None,
            'promoted': False,
        }
        if not setup_name or not pair:
            return defaults
        try:
            from db_pool import get_trading_forex
            conn = get_trading_forex()
            row = conn.execute("""
                SELECT total_trades, wins, losses, win_rate, total_usd, total_pips, promoted
                FROM setup_revenue WHERE setup_name = ? AND pair = ?
                ORDER BY total_trades DESC LIMIT 1
            """, (setup_name, pair)).fetchone()
            if not row:
                return defaults
            total_trades, wins, losses, win_rate, total_usd, total_pips, promoted = row
            # win_rate is stored as decimal (0.75) — convert to percent for prompts
            wr_pct = float(win_rate or 0) * 100.0 if (win_rate or 0) <= 1.0 else float(win_rate)
            # Profit factor needs per-row gross_win and gross_loss; approximate from total_usd
            # when only net is available: if all wins, PF=inf; if all losses, PF=0; otherwise None.
            pf = None
            if wins > 0 and losses == 0:
                pf = float('inf')
            elif wins == 0 and losses > 0:
                pf = 0.0
            return {
                'win_rate': round(wr_pct, 1),
                'trade_count': int(total_trades or 0),
                'wins': int(wins or 0),
                'losses': int(losses or 0),
                'gross_revenue': round(float(total_usd or 0), 2),
                'gross_revenue_pips': round(float(total_pips or 0), 1),
                'profit_factor': pf,
                'promoted': bool(promoted),
            }
        except Exception as _e:
            logger.debug("setup track-record lookup failed for %s/%s: %s", setup_name, pair, _e)
            return defaults

    def _get_bb_position(self, row: pd.Series) -> str:
        """Helper to get BB position string."""
        close = row['close']
        bb_upper = row.get('bb_upper', close)
        bb_lower = row.get('bb_lower', close)
        bb_middle = row.get('bb_middle', close)

        if close > bb_upper:
            return "Above Upper"
        elif close < bb_lower:
            return "Below Lower"
        elif close > bb_middle:
            return "Above Middle"
        else:
            return "Below Middle"

    def _calculate_separation_velocity(self, candles: List[Dict]) -> float:
        """Calculate separation velocity (change per candle over last 5 candles)."""
        if len(candles) < 10:
            return 0.0

        try:
            from backtester.ema_separation import calculate_ema, measure_separation

            closes = [float(c['close']) for c in candles]
            ema21 = calculate_ema(closes, 21)
            ema55 = calculate_ema(closes, 55)
            separations = measure_separation(ema21, ema55, closes)

            # Get last 5 separation values
            recent_separations = separations[-5:]
            valid_separations = [s for s in recent_separations if not (s != s or s == float('nan'))]

            if len(valid_separations) < 2:
                return 0.0

            # Calculate average change per candle
            velocity = (valid_separations[-1] - valid_separations[0]) / (len(valid_separations) - 1)
            return velocity
        except Exception as e:
            logger.warning(f"Error calculating separation velocity: {e}")
            return 0.0

    def _classify_velocity(self, velocity: float) -> str:
        """Classify velocity as fast, medium, or slow."""
        abs_velocity = abs(velocity)
        if abs_velocity > 0.05:
            return 'fast'
        elif abs_velocity > 0.01:
            return 'medium'
        else:
            return 'slow'

    def _count_candles_since_cross(self, candles: List[Dict]) -> int:
        """Count candles since last EMA 21/55 crossover."""
        try:
            from backtester.ema_separation import detect_ema_crossovers

            crossovers = detect_ema_crossovers(candles)
            if not crossovers:
                return 999  # No crossover found

            latest_cross = crossovers[-1]
            return len(candles) - 1 - latest_cross['index']
        except Exception as e:
            logger.warning(f"Error counting candles since cross: {e}")
            return 999

    def _create_alert(self, pair: str, setup_name: str, score: int, direction: str,
                     setup_data: Dict, latest_row: pd.Series) -> Dict:
        """Create an alert dictionary."""
        # Determine BB position
        close = latest_row['close']
        bb_upper = latest_row['bb_upper']
        bb_lower = latest_row['bb_lower']
        bb_middle = latest_row['bb_middle']

        if close > bb_upper:
            bb_position = "Above Upper"
        elif close < bb_lower:
            bb_position = "Below Lower"
        elif close > bb_middle:
            bb_position = "Above Middle"
        else:
            bb_position = "Below Middle"

        # Identify prominent candle patterns
        patterns = []
        pattern_cols = [
            'hammer', 'bullish_engulfing', 'morning_star', 'shooting_star',
            'bearish_engulfing', 'evening_star', 'dragonfly_doji', 'gravestone_doji'
        ]
        for col in pattern_cols:
            if col in latest_row and latest_row[col]:
                patterns.append(col.replace('_', ' ').title())

        candle_pattern = ", ".join(patterns) if patterns else "None"

        # Create reasoning text
        reasoning_parts = [
            f"Score: {score}",
            f"RSI: {latest_row.get('rsi', 0):.1f}",
            f"Stoch: {latest_row.get('stoch_k', 0):.1f}/{latest_row.get('stoch_d', 0):.1f}",
            f"BB: {bb_position}",
            f"ADX: {latest_row.get('adx', 0):.1f}"
        ]

        if patterns:
            reasoning_parts.append(f"Patterns: {candle_pattern}")

        reasoning = " | ".join(reasoning_parts)

        return {
            'timestamp': datetime.now().isoformat(),
            'pair': pair,
            'setup_name': setup_name,
            'score': score,
            'direction': direction,
            'historical_win_rate': setup_data['win_rate'],
            'historical_trade_count': setup_data['trade_count'],
            'historical_profit_factor': setup_data['profit_factor'],
            'current_rsi': latest_row.get('rsi'),
            'current_stoch_k': latest_row.get('stoch_k'),
            'current_stoch_d': latest_row.get('stoch_d'),
            'bb_position': bb_position,
            'candle_pattern': candle_pattern,
            'h4_bias': latest_row.get('h4_bias', 'unknown'),
            'reasoning': reasoning
        }

    def _store_alert(self, alert: Dict):
        """Store alert in database.
        Uses a fresh connection (not pooled) to avoid DB-locked conflicts when the
        trading cycle is concurrently writing to trading_forex.db.
        """
        import sqlite3 as _sql
        import time as _time

        _pair = alert.get('pair', 'unknown')
        _setup_name = alert.get('setup_name', 'unknown')
        _score = alert.get('score', 0)
        _direction = alert.get('direction') or 'PENDING'
        _alert_type = alert.get('alert_type', 'V4')

        # Write to v2/trading_forex.db — same DB the dashboard API reads from.
        _db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "Database", "v2", "trading_forex.db",
        )

        for _attempt in range(3):
            try:
                _conn = _sql.connect(_db_path, timeout=10, isolation_level=None)
                _conn.execute("PRAGMA journal_mode=DELETE")
                _conn.execute("PRAGMA busy_timeout=5000")
                # Compute cascade phase from composite signals
                _fan_exp = alert.get('fan_expanding', False)
                _bb_exp  = alert.get('bb_expanding', False)
                _is_ret  = alert.get('is_retracement', False)
                _both_exp = bool(_fan_exp and _bb_exp)
                _both_con = bool(not _fan_exp and not _bb_exp)
                if _both_exp:
                    _cascade_phase = 'trending'
                elif _is_ret:
                    _cascade_phase = 'retracing'
                else:
                    _cascade_phase = 'forming'

                # Fan width in pips from separation_pct
                _sep_pct = alert.get('ema_data', {}).get('separation_pct', 0) or 0
                _price   = alert.get('market_snapshot', {}).get('price', 1.0) or 1.0
                _pip_sz  = 0.01 if 'JPY' in _pair else 0.0001
                _fan_w_pips = (_sep_pct / 100) * _price / _pip_sz if _pip_sz > 0 else 0

                # BB width as pct
                _bb_upper = alert.get('market_snapshot', {}).get('bb_upper', 0) or 0
                _bb_lower = alert.get('market_snapshot', {}).get('bb_lower', 0) or 0
                _bb_w_pct = ((_bb_upper - _bb_lower) / _price * 100) if _price else 0

                _conn.execute("""
                    INSERT INTO scout_alerts (
                        timestamp, pair, setup_name, score, direction,
                        historical_win_rate, historical_trade_count, historical_profit_factor,
                        current_rsi, current_stoch_k, current_stoch_d,
                        bb_position, candle_pattern, h4_bias, reasoning, user_id, alert_type,
                        cascade_phase, fan_width_pips, fan_delta_5bar,
                        bb_width_pct, bb_delta_5bar,
                        is_retracement, both_expanding, both_contracting,
                        e100_dist_pips, story_score, checklist_score
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    alert.get('timestamp'), _pair, _setup_name, _score,
                    _direction, alert.get('historical_win_rate', 0), alert.get('historical_trade_count', 0),
                    alert.get('historical_profit_factor', 0), alert.get('current_rsi'), alert.get('current_stoch_k'),
                    alert.get('current_stoch_d'), alert.get('bb_position', ''), alert.get('candle_pattern', ''),
                    alert.get('h4_bias', 'unknown'), alert.get('reasoning', ''),
                    getattr(self, '_user_id', None), _alert_type,
                    _cascade_phase, round(_fan_w_pips, 1), alert.get('fan_delta_5bar', 0),
                    round(_bb_w_pct, 3), alert.get('bb_delta_5bar', 0),
                    1 if _is_ret else 0, 1 if _both_exp else 0, 1 if _both_con else 0,
                    alert.get('e100_distance_pips', 0),
                    alert.get('story_score', 0), alert.get('checklist_score', 0),
                ))
                _last_id = _conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                _conn.commit()
                _conn.close()
                # Put the DB rowid back on the alert so it travels through the pipeline
                alert['scout_alert_id'] = _last_id
                return  # success
            except _sql.OperationalError as _oe:
                if 'locked' in str(_oe).lower() and _attempt < 2:
                    logger.warning("[SCOUT_ALERT] DB locked on attempt %d for %s — retrying", _attempt + 1, _pair)
                    _time.sleep(0.15)
                    continue
                logger.warning("[SCOUT_ALERT] Failed to store alert for %s after %d attempts: %s",
                               _pair, _attempt + 1, _oe)
                return
            except Exception as _e:
                logger.warning("[SCOUT_ALERT] Unexpected error storing alert for %s: %s", _pair, _e)
                return
            finally:
                try:
                    _conn.close()
                except Exception:
                    pass

        # Scout triggers trade cycles via WebSocket → dashboard → auto-analyze
        # Scout does NOT create snipes. Snipes come from the Validator's HOLD conditions.
        # The WebSocket broadcast in _broadcast_alert() handles the dashboard notification.

    # _create_snipe_directly REMOVED - snipes only come from:
    # 1. Validator HOLD → watch_manager.create_watch()
    # 2. Winning trade → watch_manager.create_watch_from_win()

    def _check_snipes_for_pair(self, pair: str, bull_score: float, bear_score: float,
                                indicators, market_picture: dict,
                                market_snapshot: dict = None,
                                market_story: dict = None,
                                alert_type: str = None) -> list:
        """Check active snipes for this pair using Scout's already-computed data.

        Instead of the watch_manager making separate OANDA API calls and computing
        sniper scores independently, Scout piggybacks snipe checking onto its existing
        scan - same data, same cycle, zero extra cost.

        Now also checks market_story fields (candle structure, momentum state,
        thesis type) so validator snipe conditions align with scout analysis.

        Returns list of snipe_triggered alert dicts to broadcast.
        """
        try:
            conn = get_trading_forex()
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, conditions, raw_suggestion, context, "
                    "validator_verdict, validator_confidence, suggestion_type, workspace_task_id, "
                    "peak_progress, triggered_at, direction "
                    "FROM watch_suggestions "
                    "WHERE status = 'watching' AND instrument = ? AND user_id = ?",
                    (pair, self._user_id)
                ).fetchall()
            except sqlite3.OperationalError as _db_err:
                if 'disk I/O error' in str(_db_err) or 'database is locked' in str(_db_err):
                    # Connection is poisoned — nuke it and retry once with a fresh one
                    logger.warning("[SNIPE] %s disk I/O on pooled conn — resetting connection and retrying", pair)
                    from db_pool import _nuke_connection
                    _nuke_connection('trading_forex_conn')
                    conn = get_trading_forex()
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT id, conditions, raw_suggestion, context, "
                        "validator_verdict, validator_confidence, suggestion_type, workspace_task_id, "
                        "peak_progress, triggered_at "
                        "FROM watch_suggestions "
                        "WHERE status = 'watching' AND instrument = ? AND user_id = ?",
                        (pair, self._user_id)
                    ).fetchall()
                    logger.info("[SNIPE] %s retry succeeded with fresh connection", pair)
                else:
                    raise

            if not rows:
                return []

            triggered = []
            now_iso = datetime.now().isoformat()
            max_score = max(bull_score, bear_score)

            # Build a sniper-like result dict for check_conditions compatibility
            ind_dict = {}
            if hasattr(indicators, 'to_dict'):
                ind_dict = {k: float(v) if isinstance(v, (int, float)) else v
                           for k, v in indicators.to_dict().items()}
            elif isinstance(indicators, dict):
                ind_dict = indicators

            sniper_result = {
                "buy_score": bull_score,
                "sell_score": bear_score,
                "indicators": ind_dict,
                "detected_patterns": [k for k in ['hammer', 'bullish_engulfing', 'morning_star',
                                                    'shooting_star', 'bearish_engulfing', 'evening_star',
                                                    'dragonfly_doji', 'gravestone_doji']
                                      if ind_dict.get(k)],
            }

            ema_data = market_picture.get('ema', {}) if market_picture else {}

            for row in rows:
                watch_id = row["id"]
                try:
                    conditions = json.loads(row["conditions"])
                except (json.JSONDecodeError, TypeError):
                    continue

                # Check each condition against Scout's data
                all_met = True
                details = []

                for cond in conditions:
                    field = cond.get("field", "")
                    op = cond.get("op", ">=")
                    target = cond.get("value")
                    current = None
                    met = False

                    # ── Sniper scores ──
                    if field in ("max_score", "sniper_score"):
                        current = max_score
                    elif field == "buy_score":
                        current = bull_score
                    elif field == "sell_score":
                        current = bear_score

                    # ── EMA narrative fields ──
                    elif field == "ema_fan_state":
                        current = ema_data.get("fan_state", "unknown")
                        if op == "in" and isinstance(target, list):
                            met = current in target
                            details.append({"field": field, "current": current, "target": target, "met": met})
                            if not met: all_met = False
                            continue
                    elif field == "ema_trend_health":
                        current = ema_data.get("trend_health", 0)
                    elif field == "ema_velocity":
                        current = ema_data.get("separation_velocity", 0)
                    elif field == "ema_reversal_risk":
                        current = ema_data.get("reversal_risk", "unknown")
                        if op == "in" and isinstance(target, list):
                            met = current in target
                            details.append({"field": field, "current": current, "target": target, "met": met})
                            if not met: all_met = False
                            continue

                    # ── H4 timeframe fields ──
                    elif field == "h4_bias":
                        current = (market_snapshot or {}).get("h4_bias") or ind_dict.get("h4_bias") or ema_data.get("h4_bias", "unknown")
                        if current:
                            current = str(current).upper()
                        if isinstance(current, str):
                            current = current.upper()
                        if isinstance(target, str):
                            target = target.upper()
                    elif field == "h4_rsi":
                        current = (market_snapshot or {}).get("h4_rsi", ind_dict.get("h4_rsi"))
                        if current is not None:
                            current = float(current)

                    # ── RSI slope (computed from recent bars) ──
                    elif field == "rsi_slope":
                        current = ind_dict.get("rsi_slope")
                        if current is None:
                            # Fallback: try to compute from rsi history if available
                            current = ind_dict.get("RSI_slope", ind_dict.get("rsi_change", 0))

                    # ── Candle pattern detection ──
                    elif field == "has_reversal_pattern":
                        rev = {"hammer", "inverted_hammer", "bullish_engulfing", "bearish_engulfing",
                               "morning_star", "evening_star", "piercing_line", "dark_cloud_cover",
                               "three_white_soldiers", "three_black_crows"}
                        detected = set(p.lower().replace(" ", "_") for p in sniper_result.get("detected_patterns", []))
                        current = bool(detected & rev)
                    elif field == "has_pattern":
                        # Check for a specific named candle pattern
                        detected = set(p.lower().replace(" ", "_") for p in sniper_result.get("detected_patterns", []))
                        tgt = target.lower().replace(" ", "_") if isinstance(target, str) else ""
                        current = tgt if tgt in detected else ""

                    # ── Chart pattern detection ──
                    elif field == "has_chart_pattern":
                        chart_pats = (market_snapshot or {}).get("chart_patterns", [])
                        pat_names = [p.get("pattern", "").lower() for p in chart_pats if isinstance(p, dict)]
                        tgt = target.lower() if isinstance(target, str) else ""
                        current = tgt if tgt in pat_names else ""

                    # ── Regime detection ──
                    elif field == "regime":
                        # Use detected regime from market snapshot or compute from ADX
                        current = (market_snapshot or {}).get("regime", "")
                        if not current:
                            _adx = ind_dict.get("adx", 25)
                            _rsi = ind_dict.get("rsi", 50)
                            _bb = ind_dict.get("bb_width", 1.0)
                            if _adx > 35: current = "strong_trend"
                            elif _adx > 25 and (_rsi > 70 or _rsi < 30): current = "exhaustion"
                            elif _bb < 0.5: current = "squeeze"
                            elif _adx < 20: current = "ranging"
                            else: current = "mixed"

                    # ── Classified S1-S20 setup ──
                    elif field == "classified_setup":
                        setups = (market_snapshot or {}).get("classified_setups", [])
                        setup_ids = [s.get("setup", "") for s in setups if isinstance(s, dict)]
                        tgt = target if isinstance(target, str) else ""
                        current = tgt if tgt in setup_ids else ""

                    # ── Market session ──
                    elif field == "session":
                        from market_sessions import get_active_sessions
                        try:
                            active = [s.lower() for s in get_active_sessions()]
                        except Exception:
                            active = []
                        if op == "in" and isinstance(target, list):
                            current = [s for s in active if s in [t.lower() for t in target]]
                            met = len(current) > 0
                            details.append({"field": field, "current": active, "target": target, "met": met})
                            if not met: all_met = False
                            continue
                        current = active

                    # ── Derived zone fields (legacy) ──
                    elif field == "rsi_zone":
                        rsi = ind_dict.get("rsi", ind_dict.get("RSI", 50))
                        current = "overbought" if rsi >= 70 else "oversold" if rsi <= 30 else "neutral"
                    elif field == "stoch_zone":
                        sk = ind_dict.get("stoch_k", 50)
                        current = "overbought" if sk >= 80 else "oversold" if sk <= 20 else "neutral"
                    elif field == "bb_position":
                        close = ind_dict.get("close", 0)
                        bb_u = ind_dict.get("bb_upper", 0)
                        bb_l = ind_dict.get("bb_lower", 0)
                        bb_m = ind_dict.get("bb_middle", 0)
                        if close and bb_u and bb_l:
                            if close > bb_u: current = "Above Upper"
                            elif close < bb_l: current = "Below Lower"
                            elif close > bb_m: current = "Above Middle"
                            else: current = "Below Middle"

                    # ── Market Story fields (candle structure, momentum, thesis) ──
                    elif field == "wick_pressure":
                        story_struct = (market_story or {}).get('layers', {}).get('structure', {})
                        current = story_struct.get('wick_pressure', {}).get('dominant_pressure', 'unknown')
                    elif field == "wick_pressure_strength":
                        story_struct = (market_story or {}).get('layers', {}).get('structure', {})
                        current = story_struct.get('wick_pressure', {}).get('pressure_strength', 'unknown')
                    elif field == "e100_interaction":
                        story_struct = (market_story or {}).get('layers', {}).get('structure', {})
                        current = story_struct.get('e100_interaction', {}).get('interaction', 'unknown')
                    elif field == "e100_bounces":
                        story_struct = (market_story or {}).get('layers', {}).get('structure', {})
                        current = story_struct.get('e100_interaction', {}).get('bounces', 0)
                    elif field == "e100_breaks":
                        story_struct = (market_story or {}).get('layers', {}).get('structure', {})
                        current = story_struct.get('e100_interaction', {}).get('breaks', 0)
                    elif field == "body_trend":
                        story_struct = (market_story or {}).get('layers', {}).get('structure', {})
                        current = story_struct.get('body_trend', {}).get('body_trend', 'unknown')
                    elif field == "body_direction_bias":
                        story_struct = (market_story or {}).get('layers', {}).get('structure', {})
                        current = story_struct.get('body_trend', {}).get('direction_bias', 'unknown')
                    elif field == "range_trend":
                        story_struct = (market_story or {}).get('layers', {}).get('structure', {})
                        current = story_struct.get('consecutive', {}).get('range_trend', 'unknown')
                    elif field == "run_state":
                        story_struct = (market_story or {}).get('layers', {}).get('structure', {})
                        current = story_struct.get('consecutive', {}).get('run_state', 'unknown')
                    elif field == "momentum_state":
                        story_mom = (market_story or {}).get('layers', {}).get('momentum', {})
                        current = story_mom.get('state', 'unknown')
                    elif field == "momentum_exhausted":
                        story_mom = (market_story or {}).get('layers', {}).get('momentum', {})
                        current = story_mom.get('exhausted', False)
                    elif field == "momentum_significance":
                        story_mom = (market_story or {}).get('layers', {}).get('momentum', {})
                        current = story_mom.get('significance', 'unknown')
                    elif field == "story_entry_type":
                        current = (market_story or {}).get('entry_type', 'none')
                    elif field == "story_opportunity_score":
                        current = (market_story or {}).get('opportunity_score', 0)
                    elif field == "story_has_opportunity":
                        current = (market_story or {}).get('has_opportunity', False)
                    elif field == "price_position":
                        story_struct = (market_story or {}).get('layers', {}).get('structure', {})
                        current = story_struct.get('candle_structure', {}).get('ema_interaction', {}).get('price_position', 'unknown')

                    # ── Bollinger bandwidth fields ──
                    elif field == "bb_width":
                        bb = (market_picture or {}).get('bollinger', {})
                        current = bb.get('bb_width') or bb.get('bandwidth')
                        if current is None:
                            # Fallback: compute from indicators
                            _bbu = ind_dict.get('bb_upper', 0)
                            _bbl = ind_dict.get('bb_lower', 0)
                            _bbm = ind_dict.get('bb_middle', ind_dict.get('sma_20', 0))
                            if _bbm and _bbu and _bbl:
                                current = (_bbu - _bbl) / _bbm
                            else:
                                current = ind_dict.get('bb_width', ind_dict.get('bb_bandwidth', 0))
                        if current is not None:
                            try: current = float(current)
                            except (TypeError, ValueError): current = 0
                    elif field == "bb_expanding":
                        bb = (market_picture or {}).get('bollinger', {})
                        current = bb.get('bb_expanding', False)
                    elif field == "bb_contracting":
                        bb = (market_picture or {}).get('bollinger', {})
                        current = bb.get('bb_contracting', False)
                    elif field == "bb_acceleration":
                        bb = (market_picture or {}).get('bollinger', {})
                        current = bb.get('bb_acceleration', 0)
                    elif field == "bb_width_trend":
                        bb = (market_picture or {}).get('bollinger', {})
                        current = bb.get('bb_width_trend', 'stable')
                        if op == "in" and isinstance(target, list):
                            met = current in target
                            details.append({"field": field, "current": current, "target": target, "met": met})
                            if not met: all_met = False
                            continue

                    # ── Confluence (auto-pass, needs full cycle) ──
                    elif field == "total_score":
                        details.append({"field": field, "current": "N/A", "target": target, "met": True,
                                       "note": "confluence requires full cycle - auto-pass"})
                        continue

                    # ── Direct indicator lookup (fallback) ──
                    elif field in ind_dict:
                        current = ind_dict.get(field)

                    # Generic comparison
                    if current is not None and target is not None:
                        if op == ">=": met = current >= target
                        elif op == ">": met = current > target
                        elif op == "<=": met = current <= target
                        elif op == "<": met = current < target
                        elif op == "==": met = current == target
                        elif op == "in": met = current in target if isinstance(target, list) else current == target

                    if not met:
                        all_met = False
                    details.append({"field": field, "current": current, "target": target, "met": met})

                # ── Progress tracking + threshold trigger ──
                SNIPE_TRIGGER_THRESHOLD = tc_get("scout.snipe_trigger_threshold", 0.90)  # raised 0.80→0.90 (2026-03-11): both GBP/NZD losses had RSI>55 as missing 5th condition at 80%
                met_count = sum(1 for d in details if d.get("met"))
                total_count = len(details) if details else 1
                progress_pct = met_count / total_count if total_count > 0 else 0
                old_peak = 0
                try:
                    old_peak = float(row["peak_progress"] or 0)
                except Exception as e:
                    logging.warning("[SCOUT] Failed to parse peak_progress: %s", e)
                new_peak = max(old_peak, progress_pct)

                # Store progress on every check
                progress_json = json.dumps(details)
                conn.execute(
                    "UPDATE watch_suggestions SET conditions_progress=?, conditions_met_count=?, "
                    "conditions_total_count=?, peak_progress=?, last_checked_at=?, check_count=check_count+1 WHERE id=?",
                    (progress_json, met_count, total_count, new_peak, now_iso, watch_id)
                )

                # ── P1 Criteria grading — weekly hit-rate tracking ────────────────────
                # Count this scan; credit it if ≥50% of conditions are currently met.
                # After 100+ scans, flag stale if hit_rate drops below 50%.
                # Clear the flag automatically if hit_rate recovers to ≥60%.
                _criteria_credit = 1 if progress_pct >= 0.5 else 0
                conn.execute(
                    """UPDATE watch_suggestions
                       SET criteria_scan_count = COALESCE(criteria_scan_count, 0) + 1,
                           criteria_met_count  = COALESCE(criteria_met_count,  0) + ?,
                           criteria_hit_rate   = CAST(COALESCE(criteria_met_count, 0) + ? AS REAL)
                                                 / (COALESCE(criteria_scan_count, 0) + 1),
                           last_graded_at      = ?
                       WHERE id = ?""",
                    (_criteria_credit, _criteria_credit, now_iso, watch_id)
                )
                conn.execute(
                    """UPDATE watch_suggestions
                       SET stale_flagged_at = COALESCE(stale_flagged_at, ?)
                       WHERE id = ?
                         AND criteria_scan_count >= 100
                         AND criteria_hit_rate < 0.50
                         AND stale_flagged_at IS NULL""",
                    (now_iso, watch_id)
                )
                conn.execute(
                    "UPDATE watch_suggestions SET stale_flagged_at = NULL "
                    "WHERE id = ? AND criteria_hit_rate >= 0.60",
                    (watch_id,)
                )

                # Commit progress updates per watch — don't hold RESERVED lock across loop
                conn.commit()

                should_trigger = conditions and (all_met or progress_pct >= SNIPE_TRIGGER_THRESHOLD)

                if progress_pct > 0 and not should_trigger:
                    logger.info("📊 Snipe %s #%d: %d/%d conditions met (%.0f%%) - peak %.0f%%",
                               pair, watch_id, met_count, total_count, progress_pct * 100, new_peak * 100)

                if should_trigger:
                    trigger_type = "full" if all_met else f"threshold ({met_count}/{total_count} = {progress_pct:.0%})"

                    # Check cooldown — don't re-trigger within 30 minutes of last trigger
                    _last_trigger = row["triggered_at"]
                    _cooldown_ok = True
                    if _last_trigger:
                        try:
                            from datetime import timezone as _tz
                            _lt = datetime.fromisoformat(_last_trigger.replace('Z', '+00:00'))
                            _now = datetime.now(_tz.utc)
                            _mins_since = (_now - _lt).total_seconds() / 60
                            if _mins_since < 30:
                                logger.info("⏳ Snipe %s #%d: 4/5 but cooldown (%.0f min since last trigger, need 30)",
                                           pair, watch_id, _mins_since)
                                _cooldown_ok = False
                        except Exception:
                            pass  # Can't parse — allow trigger

                    if not _cooldown_ok:
                        continue

                    # Record trigger time and mark as triggered
                    conn.execute(
                        "UPDATE watch_suggestions SET triggered_at=?, status='triggered' WHERE id=?",
                        (now_iso, watch_id)
                    )

                    # Build snipe context from the watch's stored context + Scout's live data
                    snipe_ctx = {}
                    try:
                        snipe_ctx = json.loads(row["context"]) if row["context"] else {}
                    except Exception as e:
                        logging.warning("[SCOUT] Failed to parse watch context: %s", e)

                    # Pull validator metadata from DB row
                    snipe_ctx["validator_verdict"] = row["validator_verdict"] or "HOLD"
                    snipe_ctx["validator_confidence"] = row["validator_confidence"] or 0
                    snipe_ctx["raw_suggestion"] = row["raw_suggestion"]
                    snipe_ctx["suggestion_type"] = row["suggestion_type"] or "unknown"
                    snipe_ctx["watch_id"] = watch_id
                    snipe_ctx["trigger_type"] = trigger_type
                    snipe_ctx["conditions_progress"] = progress_pct
                    # 2026-04-23: HONOR KRONOS DIRECTION for kronos_path_snipe watches.
                    # Kronos predicts reversals; scout's live snipe context defaults to
                    # current-trend direction which is OPPOSITE of what kronos predicted.
                    # Same fix we applied to _fire_snipe_cycle in trading_api_routes.py,
                    # but scout fires through its own path (_queue_snipe_cycles) so needs
                    # independent handling. For validator_structured watches, keep existing
                    # behavior (direction flows from context).
                    if row["suggestion_type"] == "kronos_path_snipe":
                        _wdir_db = (row["direction"] or "").upper()
                        if _wdir_db in ("BUY", "SELL"):
                            snipe_ctx["direction"] = _wdir_db
                            snipe_ctx["re_entry_direction"] = _wdir_db
                            logger.info("[SCOUT] Kronos path snipe #%d: honoring kronos direction %s",
                                        watch_id, _wdir_db)

                    # Enrich with Scout's current data
                    snipe_ctx["scout_bull_score"] = bull_score
                    snipe_ctx["scout_bear_score"] = bear_score
                    snipe_ctx["scout_max_score"] = max_score
                    snipe_ctx["conditions_met"] = details
                    snipe_ctx["triggered_by"] = "snipe"
                    # Safety net: setup_id should already be in snipe_ctx from watch context.
                    # If missing, it means the watch was created before the pipeline fix — use alert_type.
                    if not snipe_ctx.get("setup_id"):
                        logger.warning("[SNIPE] Watch #%s missing setup_id in context — using alert_type fallback", watch_id)
                        snipe_ctx["setup_id"] = alert_type or f"snipe_watch_{watch_id}"
                        snipe_ctx["setup_name"] = alert_type or f"snipe_watch_{watch_id}"
                    if market_snapshot:
                        snipe_ctx["market_snapshot"] = market_snapshot

                    triggered.append({
                        "type": "snipe_triggered",
                        "pair": pair,
                        "watch_id": watch_id,
                        "instrument": pair,
                        "suggestion_type": row["suggestion_type"] or "unknown",
                        "raw_suggestion": row["raw_suggestion"],
                        "conditions_met": details,
                        "snipe_context": snipe_ctx,
                        "market_snapshot": market_snapshot,
                    })

                    logger.info("🎯 SNIPE TRIGGERED (%s) by Scout for %s (watch_id=%d): %s",
                               trigger_type, pair, watch_id, row["raw_suggestion"])

            conn.commit()
            # Don't close pooled connections from get_trading_forex()

            # Queue cycles for triggered snipes via the server API
            if triggered:
                self._queue_snipe_cycles(triggered)

            return triggered

        except Exception as exc:
            logger.error("Snipe check error for %s: %s", pair, exc)
            try:
                conn.rollback()
            except Exception:
                pass
            return []

    def _queue_scout_cycle(self, pair: str, alert: dict):
        """Queue a normal-priority cycle for a scout opportunity via HTTP to serve_ui."""
        import urllib.request
        try:
            scout_context = alert.get("market_snapshot", {})
            # Inject top-level alert fields that market_snapshot doesn't carry
            scout_context["setup_id"] = alert.get("setup_id")
            scout_context["setup_name"] = alert.get("setup_name")
            scout_context["direction"] = alert.get("direction")
            scout_context["win_rate"] = alert.get("historical_win_rate", 0)
            scout_context["trade_count"] = alert.get("historical_trade_count", 0)
            scout_context["profit_factor"] = alert.get("historical_profit_factor", 0)
            scout_context["scout_confidence"] = alert.get("scout_confidence", 0)
            scout_context["confidence_tier"] = alert.get("confidence_tier", "")
            scout_context["queued_at"] = time.time()  # staleness tracking
            # Add finding_id for scout learning linkage
            scout_context["finding_id"] = alert.get("finding_id")
            scout_context["scout_alert_id"] = alert.get("scout_alert_id")  # links back to scout_alerts DB row
            # Divergence data for all paths
            scout_context["divergence"] = alert.get("divergence", {})
            scout_context["divergence_types"] = alert.get("divergence", {}).get("divergence_types", [])
            # Alert-level fields needed by Gate 1 sanity check in trading_cycle
            scout_context["alert_type"] = alert.get("alert_type", "")
            scout_context["is_retracement"] = alert.get("is_retracement", False)
            scout_context["triggered_by"] = alert.get("triggered_by", "scout")
            # Ensure fan_direction survives even if market_snapshot had None
            if not scout_context.get("fan_direction"):
                scout_context["fan_direction"] = alert.get("fan_direction") or \
                    alert.get("ema_data", {}).get("fan_direction", "")
            # Market story fields — critical for validator snipe conditions and decision logging
            scout_context["story_score"] = alert.get("story_score", alert.get("score", 0))
            scout_context["story_thesis"] = alert.get("story_thesis", alert.get("reasoning", ""))
            scout_context["story_entry_type"] = alert.get("story_entry_type", alert.get("entry_type", ""))
            scout_context["story_narrative"] = alert.get("story_narrative", "")
            scout_context["story_confidence"] = alert.get("story_confidence", 0)
            scout_context["entry_type"] = alert.get("story_entry_type", alert.get("entry_type", ""))
            scout_context["opportunity_score"] = alert.get("story_score", alert.get("score", 0))
            scout_context["classified_setups"] = alert.get("classified_setups", [])
            # Path D middle-zone fields
            if alert.get("_path_d_id"):
                scout_context["_path_d_id"] = alert["_path_d_id"]
                scout_context["_path_d_setup"] = alert.get("_path_d_setup", "")
                scout_context["_path_d_wr"] = alert.get("_path_d_wr", 0)
                scout_context["_path_d_trades"] = alert.get("_path_d_trades", 0)
                scout_context["_path_d_regime"] = alert.get("_path_d_regime", "")
                scout_context["_path_d_divergence"] = alert.get("_path_d_divergence", "")
                scout_context["_path_d_playbook"] = alert.get("_path_d_playbook", {})
            payload = json.dumps({
                "pair": pair,
                "source": "scout",
                "scout_context": scout_context,
            }).encode()
            req = urllib.request.Request(
                "http://localhost:8766/api/trading/run-cycle",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
                logger.info("Scout cycle queued for %s: %s", pair, result.get("status", "?"))
        except Exception as exc:
            logger.warning("Failed to queue scout cycle for %s: %s", pair, exc)

    def _flush_stale_scout_entries(self):
        """Flush stale scout entries from queue before a new scan cycle.
        Snipe entries are preserved - only source='scout' entries get cleared."""
        import urllib.request
        try:
            # Call the flush endpoint which clears only scout-source entries
            payload = json.dumps({"source": "scout", "user_id": self._user_id}).encode()
            req = urllib.request.Request(
                "http://localhost:8766/api/trading/flush-stale",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                result = json.loads(resp.read())
                flushed = result.get("flushed", 0)
                if flushed > 0:
                    logger.info("Flushed %d stale scout entries from queue", flushed)
        except Exception as exc:
            logger.debug("Flush stale call failed (non-critical): %s", exc)

    def _queue_snipe_cycles(self, triggered_snipes: list):
        """Queue high-priority cycles for triggered snipes via HTTP to serve_ui."""
        import urllib.request
        for snipe in triggered_snipes:
            try:
                payload = json.dumps({
                    "pair": snipe["pair"],
                    "source": "snipe",
                    "scout_context": snipe.get("snipe_context", {}),
                }).encode()
                req = urllib.request.Request(
                    "http://localhost:8766/api/trading/run-cycle",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    result = json.loads(resp.read())
                    logger.info("Snipe cycle queued for %s: %s", snipe["pair"], result)
            except Exception as exc:
                logger.warning("Failed to queue snipe cycle for %s: %s", snipe["pair"], exc)

    async def _broadcast_alert(self, alert: Dict):
        """Broadcast alert to connected dashboard clients."""
        if not self.websocket_clients:
            return
        _ws_start = time.time()

        # Determine alert type for WebSocket
        if alert.get('type') == 'market_picture':
            alert_type = 'market_picture'
        elif alert.get('setup_name') == 'EMA_Separation':
            alert_type = 'ema_separation'
        else:
            alert_type = 'scout_alert'

        if alert_type == 'market_picture':
            message_data = {
                'type': 'market_picture',
                'pair': alert['pair'],
                'reason': alert.get('reason', ''),
                'direction': alert.get('direction', 'neutral'),
                'fan_direction': alert.get('fan_direction', 'mixed'),
                'fan_state': alert.get('fan_state', 'unknown'),
                'separation_pct': alert.get('separation_pct', 0),
                'velocity': alert.get('velocity', 0),
                'velocity_trend': alert.get('velocity_trend', 'unknown'),
                'trend_health': alert.get('trend_health', 0),
                'reversal_risk': alert.get('reversal_risk', 'unknown'),
                'rsi': alert.get('rsi'),
                'rsi_zone': alert.get('rsi_zone', 'neutral'),
                'stoch_zone': alert.get('stoch_zone', 'neutral'),
                'bb_squeeze': alert.get('bb_squeeze', False),
                'ema_narrative': alert.get('ema_narrative', ''),
                'confluence_narrative': alert.get('confluence_narrative', ''),
                'recommended_bias': alert.get('recommended_bias', 'neutral'),
            }
        elif alert_type == 'ema_separation':
            message_data = {
                'type': alert_type,
                'pair': alert['pair'],
                'signal': alert['direction'].lower(),
                'phase': alert.get('ema_data', {}).get('phase', 'unknown'),
                'separation_pct': alert.get('ema_data', {}).get('separation_pct', 0),
                'ema100_role': alert.get('ema_data', {}).get('ema100_role', 'neutral'),
                'strength': alert['score']
            }
        else:
            message_data = alert

        message = json.dumps({
            'type': alert_type,
            'data': message_data
        }, default=str)

        # Send to all connected clients (with timeout to prevent blocking)
        disconnected = set()
        for client in self.websocket_clients:
            try:
                await asyncio.wait_for(client.send(message), timeout=5)
            except Exception:
                disconnected.add(client)

        # Remove disconnected clients
        self.websocket_clients -= disconnected

        flight.record(FlightStage.DASHBOARD_WS, pair=alert.get('pair', ''), data={
            "clients": len(self.websocket_clients),
            "alert_type": alert.get('type', 'scout_alert'),
            "disconnected": len(disconnected),
        }, duration_ms=(time.time() - _ws_start) * 1000)

    def _record_scout_finding(self, alert: Dict, ema_signal: Dict, mkt_picture: Dict, session_quality: float) -> Optional[int]:
        """Record a scout finding for learning analysis using scout_learning_system.
        
        Returns:
            finding_id: ID of the created finding record, or None if failed
        """
        try:
            # Import here to avoid circular imports
            from scout_learning_system import record_scout_finding
            
            # Calculate confidence score based on multiple factors  
            confidence = self._calculate_confidence_score(alert, ema_signal, session_quality)
            
            # Prepare alert data for scout_learning_system format
            enriched_alert = alert.copy()
            # V4 sets direction=None, derive from fan_direction for scout_findings logging
            _fan_dir = alert.get('fan_direction') or ema_signal.get('fan_direction', '')
            if not enriched_alert.get('direction') and _fan_dir:
                enriched_alert['direction'] = 'BULL' if _fan_dir == 'bullish' else (
                    'BEAR' if _fan_dir == 'bearish' else 'neutral')
            enriched_alert.update({
                'scout_confidence': confidence,
                'current_rsi': alert.get('current_rsi', 50),
                'current_stoch_k': alert.get('current_stoch_k', 50),
                'bb_position': alert.get('bb_position', 'neutral'),
                'candle_pattern': alert.get('candle_pattern'),
                'h4_bias': alert.get('h4_bias'),
                'timestamp': datetime.now().isoformat(),
                'type': alert.get('alert_type', 'scout_alert'),
                'fan_state': ema_signal.get('fan_state'),
                'confluence_score': alert.get('checklist_score', alert.get('score', 0)),
                # Cascade/retracement pattern fields (Scout revamp)
                'dual_cross_cascade': alert.get('dual_cross_cascade', False),
                'cascade_direction': alert.get('cascade_direction'),
                'retracement_type': alert.get('retracement_type'),
                'bb_re_expanding': alert.get('bb_re_expanding', False),
                # Setup revenue data (winning trade feedback loop)
                'pair_gross_revenue': alert.get('live_history', {}).get('pair_gross_revenue', 0),
                'pair_best_setup': alert.get('live_history', {}).get('pair_best_setup'),
                'pair_best_setup_wr': alert.get('live_history', {}).get('pair_best_setup_wr', 0),
            })
            
            # Use the proper scout_learning_system function.
            # user_id passed as provenance; scout_findings is collective (no read filtering).
            finding_id = record_scout_finding(enriched_alert, session_quality,
                                              user_id=getattr(self, '_user_id', None))
            logger.info(f"Recorded scout finding #{finding_id} for {alert['pair']} {alert['setup_name']}")
            return finding_id
            
        except Exception as e:
            logger.warning(f"Failed to record scout finding: {e}")
            return None

    def _calculate_confidence_score(self, alert: Dict, ema_signal: Dict, session_quality: float) -> float:
        """Calculate tiered confidence score based on win rate and EMA state (TIM'S HIERARCHY)."""

        # TIER 1: Playbook confidence (80%+ win rate tiers)
        win_rate = alert.get('historical_win_rate', 80)

        if win_rate >= 90:
            base_confidence = 0.85  # Elite confidence
            tier = 'ELITE'
        elif win_rate >= 85:
            base_confidence = 0.70  # Elevated confidence
            tier = 'ELEVATED'
        else:  # 80-84%
            base_confidence = 0.55  # Base confidence
            tier = 'BASE'

        # TIER 2: EMA multiplier (primary signal)
        ema_multiplier = self._calculate_ema_multiplier(ema_signal)

        # Apply EMA multiplier to playbook confidence
        ema_adjusted_confidence = base_confidence * ema_multiplier

        # TIER 3: Session quality adjustment
        session_boost = (session_quality - 0.5) * 0.15  # ±15% based on session

        # TIER 4: Sniper score validation
        score = alert.get('score', 0)
        score_boost = min((score - 8) / 12.0, 0.1) if score > 8 else -0.1  # Up to +10% for strong scores

        final_confidence = ema_adjusted_confidence + session_boost + score_boost
        final_confidence = max(0.0, min(1.0, final_confidence))  # Clamp to 0-1

        # Store tier info for logging
        alert['confidence_tier'] = tier
        alert['ema_multiplier'] = round(ema_multiplier, 3)
        alert['base_confidence'] = round(base_confidence, 3)
        alert['final_confidence'] = round(final_confidence, 3)

        return final_confidence

    def _calculate_ema_multiplier(self, ema_signal: Dict) -> float:
        """Calculate EMA state multiplier (Tim's EMA-first approach)."""
        fan_state = ema_signal.get('fan_state', 'unknown')
        fan_direction = ema_signal.get('fan_direction', 'unknown')
        trend_health = ema_signal.get('trend_health', 0)
        velocity = abs(ema_signal.get('separation_velocity', 0))
        reversal_risk = ema_signal.get('reversal_risk', 'unknown')

        # Base multiplier
        multiplier = 1.0

        # Fan state influence (strongest factor)
        fan_multipliers = {
            'expanding': 1.3,      # Strong trend, boost confidence
            'accelerating': 1.4,   # Accelerating trend, highest boost
            'stable': 1.1,         # Steady trend, slight boost
            'decelerating': 0.9,   # Weakening trend, slight penalty
            'peaked': 0.8,         # Trend peaked, moderate penalty
            'contracting': 0.7,    # Trend dying, significant penalty
            'just_crossed': 1.2,   # Fresh signal, good boost
            'unknown': 0.9         # Unknown state, slight penalty
        }
        multiplier *= fan_multipliers.get(fan_state, 0.9)

        # Trend health influence (0-100 scale)
        health_factor = trend_health / 100.0
        if health_factor > 0.7:
            multiplier *= 1.15  # Strong trend structure
        elif health_factor < 0.3:
            multiplier *= 0.85  # Weak trend structure

        # Velocity influence (higher velocity = more conviction)
        if velocity > 0.01:
            multiplier *= 1.1   # Good momentum
        elif velocity < 0.003:
            multiplier *= 0.9   # Low momentum, could be fakeout

        # Reversal risk influence
        if reversal_risk == 'high':
            multiplier *= 0.8   # High reversal risk, reduce confidence
        elif reversal_risk == 'low':
            multiplier *= 1.1   # Low reversal risk, boost confidence

        # Clamp multiplier to reasonable bounds
        return max(0.5, min(1.5, multiplier))

    def _get_playbook_tier_stats(self) -> Dict[str, int]:
        """Get breakdown of playbook setups by win rate tiers."""
        tiers = {'80-84%': 0, '85-89%': 0, '90%+': 0}

        for setup in getattr(self, 'playbook_setups', []):
            wr = setup.get('win_rate', 0)
            if wr >= 90:
                tiers['90%+'] += 1
            elif wr >= 85:
                tiers['85-89%'] += 1
            else:
                tiers['80-84%'] += 1

        return tiers

    def get_scout_learning_insights(self, pair: str = None, lookback_hours: int = 168) -> Dict[str, Any]:
        """Get learning insights from scout findings."""
        try:
            conn = get_trading_forex()
            # Get recent findings for analysis
            since_time = (datetime.now() - timedelta(hours=lookback_hours)).isoformat()

            where_clause = "WHERE created_at > ?"
            params = [since_time]

            if pair:
                where_clause += " AND pair = ?"
                params.append(pair)

            # Analyze performance by setup type and EMA state
            query = f"""
                SELECT
                    setup_type,
                    ema_fan_state,
                    COUNT(*) as total_findings,
                    AVG(confidence_score) as avg_confidence,
                    AVG(session_quality_score) as avg_session_quality,
                    AVG(CASE WHEN snipe_triggered = 1 THEN 1.0 ELSE 0.0 END) as trigger_rate,
                    AVG(CASE WHEN outcome = 'win' THEN 1.0 ELSE 0.0 END) as success_rate,
                    AVG(pips_result) as avg_pips
                FROM scout_findings
                {where_clause}
                GROUP BY setup_type, ema_fan_state
                ORDER BY total_findings DESC
            """

            results = conn.execute(query, params).fetchall()

            insights = {
                'analysis_period_hours': lookback_hours,
                'pair_filter': pair,
                'setup_performance': [],
                'best_conditions': None,
                'worst_conditions': None,
                'recommendations': []
            }

            for row in results:
                perf = {
                    'setup_type': row[0],
                    'ema_fan_state': row[1],
                    'total_findings': row[2],
                    'avg_confidence': round(row[3] or 0, 3),
                    'avg_session_quality': round(row[4] or 0, 3),
                    'trigger_rate': round(row[5] or 0, 3),
                    'success_rate': round(row[6] or 0, 3),
                    'avg_pips': round(row[7] or 0, 1)
                }
                insights['setup_performance'].append(perf)

            # Find best and worst performing conditions
            if insights['setup_performance']:
                sorted_by_success = sorted(insights['setup_performance'],
                                         key=lambda x: x['success_rate'], reverse=True)
                insights['best_conditions'] = sorted_by_success[0] if sorted_by_success else None
                insights['worst_conditions'] = sorted_by_success[-1] if len(sorted_by_success) > 1 else None

            # Generate recommendations
            insights['recommendations'] = self._generate_scout_recommendations(insights['setup_performance'])

            return insights

        except Exception as e:
            logger.error(f"Error getting scout learning insights: {e}")
            return {'error': str(e)}

    def _generate_scout_recommendations(self, performance_data: List[Dict]) -> List[str]:
        """Generate actionable recommendations from performance data."""
        recommendations = []

        if not performance_data:
            return ["Need more data to generate recommendations"]

        # Find patterns in the data
        high_success = [p for p in performance_data if p['success_rate'] > 0.7]
        low_success = [p for p in performance_data if p['success_rate'] < 0.3 and p['total_findings'] > 5]

        if high_success:
            best = max(high_success, key=lambda x: x['success_rate'])
            recommendations.append(
                f"Focus on {best['setup_type']} during {best['ema_fan_state']} EMA states "
                f"(success rate: {best['success_rate']:.1%})"
            )

        if low_success:
            worst = min(low_success, key=lambda x: x['success_rate'])
            recommendations.append(
                f"Avoid {worst['setup_type']} during {worst['ema_fan_state']} EMA states "
                f"(success rate: {worst['success_rate']:.1%})"
            )

        # Session quality insights
        high_session = [p for p in performance_data if p['avg_session_quality'] > 0.8]
        if high_session:
            recommendations.append("Prime session times show better performance - prioritize those alerts")

        # Confidence calibration
        overconfident = [p for p in performance_data if p['avg_confidence'] > p['success_rate'] + 0.2]
        if overconfident:
            recommendations.append("Confidence scoring may be overoptimistic - recalibrate thresholds")

        return recommendations

    def get_scout_status(self) -> Dict[str, Any]:
        """Get current scout status for API endpoint."""
        try:
            # Get recent alerts from database
            conn = get_trading_forex()
            # Get alerts from last 24 hours
            yesterday = (datetime.now() - timedelta(hours=24)).isoformat()

            recent_alerts = conn.execute("""
                SELECT pair, setup_name, direction, timestamp, score,
                       historical_win_rate, reasoning
                FROM scout_alerts
                WHERE timestamp > ?
                ORDER BY timestamp DESC
                LIMIT 50
            """, (yesterday,)).fetchall()

            # Get high separation pairs from recent EMA alerts
            high_separation_pairs = []
            ema_alerts = []

            for alert in recent_alerts:
                if alert[1] == 'EMA_Separation':  # setup_name
                    ema_alerts.append({
                        'pair': alert[0],
                        'direction': alert[2],
                        'timestamp': alert[3],
                        'score': alert[4],
                        'reasoning': alert[6]
                    })

                    # Parse separation percentage from reasoning
                    reasoning = alert[6]
                    if 'ELITE BOOST' in reasoning:
                        import re
                        sep_match = re.search(r'(\d+\.\d+)% separation', reasoning)
                        if sep_match:
                            sep_pct = float(sep_match.group(1))
                            vel_match = re.search(r'Velocity: (\w+)', reasoning)
                            velocity = vel_match.group(1) if vel_match else 'unknown'

                            high_separation_pairs.append({
                                'pair': alert[0],
                                'separation_pct': sep_pct,
                                'velocity': velocity,
                                'direction': alert[2]
                            })

            next_scan_in = self.scan_interval  # Approximate next scan time

            # Determine if scout is actively running by checking for recent DB scans
            _is_running = self.running
            if not _is_running:
                try:
                    last_row = conn.execute(
                        "SELECT timestamp FROM scout_alerts ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()
                    if last_row:
                        from datetime import datetime as _dt
                        _last_ts = _dt.fromisoformat(last_row[0].replace('Z', '+00:00')) if 'Z' in last_row[0] else _dt.fromisoformat(last_row[0])
                        _age = (_dt.now() - _last_ts.replace(tzinfo=None)).total_seconds()
                        _is_running = _age < 300  # active if scan within 5 min
                except Exception:
                    pass

            return {
                'last_scan': datetime.now(tz=timezone.utc).isoformat(),
                'pairs_scanned': len(self.pairs),
                'high_separation_pairs': high_separation_pairs,
                'active_alerts': ema_alerts,
                'next_scan_in': next_scan_in,
                'running': _is_running,
                'playbook_setups_total': len(self.playbook_setups),
                'playbook_tiers': self._get_playbook_tier_stats()
            }

        except Exception as e:
            logger.error(f"Error getting scout status: {e}")
            return {
                'last_scan': datetime.now(tz=timezone.utc).isoformat(),
                'pairs_scanned': len(self.pairs),
                'high_separation_pairs': [],
                'active_alerts': [],
                'next_scan_in': self.scan_interval,
                'running': self.running,
                'error': str(e)
            }

    def _get_bb_position(self, latest_row):
        """Helper method to determine Bollinger Band position for profile matching."""
        bb_upper = latest_row.get('bb_upper', 0)
        bb_middle = latest_row.get('bb_middle', 0)
        bb_lower = latest_row.get('bb_lower', 0)
        close_price = latest_row.get('close', 0)

        if bb_upper == 0 or bb_lower == 0 or bb_middle == 0:
            return 'unknown'

        if close_price > bb_upper:
            return 'above_upper'
        elif close_price > bb_middle:
            return 'mid_to_upper'
        elif close_price > bb_lower:
            return 'lower_to_mid'
        else:
            return 'below_lower'

    def _get_current_regime(self, latest_row):
        """Helper method to determine current market regime for profile matching.
        NOTE: 'mixed' does NOT exist in backtest DB — map to real regimes only."""
        adx_value = latest_row.get('ADX', 25)
        rsi = latest_row.get('RSI', latest_row.get('rsi', 50))
        bb_width = latest_row.get('bb_width', 0.005)

        if bb_width and bb_width < 0.003:
            return 'squeeze'
        if adx_value > 25:
            # ADX 25+ = trending (was 35 — too strict, missed real trends)
            if rsi > 70 or rsi < 30:
                return 'exhaustion'
            return 'strong_trend'
        if adx_value < 20:
            return 'ranging'
        # ADX 20-25 = transitional, classify as ranging
        return 'ranging'

    def _compute_expansion_quality(self, row, direction, opportunity_source):
        """Score expansion entry quality 0-14 based on 8.5M backtest trades.
        
        Higher = earlier in the cross event = better timing = higher WR.
        This is NOT a direction signal — it measures HOW GOOD the expansion entry is.
        
        Components (all measure TIMING — how early you are to the cross):
          RSI sweet spot:     +3 (sell 30-50 / buy 50-70) → BT: 82-98% WR
          MACD just crossing: +3 (opposing direction)      → BT: 87.7% WR  
          CCI mild:           +2 (-100 to 100)             → BT: 88.3% WR
          ADX slope fresh:    +3 (slope < 2)               → BT: 89-92% WR
          BB width wide:      +3 (> 0.007)                 → BT: avg +32p
        """
        if opportunity_source not in ('expansion_thesis', 'expansion_thesis_mixed'):
            return None
        
        score = 0
        details = {}
        d = direction.lower().replace('bullish', 'buy').replace('bearish', 'sell')
        
        # --- RSI timing ---
        rsi = float(row.get('rsi', row.get('RSI', 50)))
        if d == 'sell':
            if 40 <= rsi <= 50:
                score += 3; details['rsi'] = 'EARLY(40-50) +3'
            elif 30 <= rsi < 40:
                score += 2; details['rsi'] = 'good(30-40) +2'
            elif 20 <= rsi < 30:
                score += 0; details['rsi'] = 'late(20-30) +0'
            else:
                score -= 2; details['rsi'] = f'spent/wrong({rsi:.0f}) -2'
        else:  # buy
            if 50 <= rsi <= 60:
                score += 3; details['rsi'] = 'EARLY(50-60) +3'
            elif 60 < rsi <= 70:
                score += 3; details['rsi'] = 'SWEET(60-70) +3'  # BT: 85.3% = best buy zone
            elif 70 < rsi <= 85:
                score += 1; details['rsi'] = 'good(70-85) +1'
            else:
                score -= 2; details['rsi'] = f'spent/wrong({rsi:.0f}) -2'
        
        # --- MACD timing (opposing = AT the crossover = best) ---
        macd_h = float(row.get('macd_histogram', row.get('macd_hist', 0)))
        if d == 'sell':
            if macd_h > 0:        # opposing (positive) = at cross
                score += 3; details['macd'] = f'AT_CROSS(+{macd_h:.5f}) +3'
            elif macd_h > -0.005: # near zero = early
                score += 2; details['macd'] = f'early({macd_h:.5f}) +2'
            else:                  # deep agreement = after cross
                score += 0; details['macd'] = f'after({macd_h:.5f}) +0'
        else:
            if macd_h < 0:
                score += 3; details['macd'] = f'AT_CROSS({macd_h:.5f}) +3'
            elif macd_h < 0.005:
                score += 2; details['macd'] = f'early({macd_h:.5f}) +2'
            else:
                score += 0; details['macd'] = f'after({macd_h:.5f}) +0'
        
        # --- CCI timing (mild = early) ---
        cci = float(row.get('cci', row.get('CCI', 0)))
        if -100 <= cci <= 100:
            score += 2; details['cci'] = f'mild({cci:.0f}) +2'
        else:
            score += 0; details['cci'] = f'extreme({cci:.0f}) +0'
        
        # --- ADX slope (fresh trend = best) ---
        adx_slope = row.get('adx_slope', None)
        if adx_slope is not None:
            adx_slope = float(adx_slope)
            if adx_slope < 0:
                score += 3; details['adx_slope'] = f'falling({adx_slope:.1f}) +3'
            elif adx_slope < 2:
                score += 3; details['adx_slope'] = f'fresh({adx_slope:.1f}) +3'
            elif adx_slope < 4:
                score += 0; details['adx_slope'] = f'normal({adx_slope:.1f}) +0'
            else:
                score -= 2; details['adx_slope'] = f'PEAKING({adx_slope:.1f}) -2'
        else:
            details['adx_slope'] = 'unknown'
        
        # --- BB width (bigger = bigger paycheck) ---
        bbw = float(row.get('bb_width', 0))
        if bbw > 0.007:
            score += 3; details['bb_width'] = f'very_wide({bbw:.4f}) +3'
        elif bbw > 0.004:
            score += 2; details['bb_width'] = f'wide({bbw:.4f}) +2'
        elif bbw > 0.002:
            score += 1; details['bb_width'] = f'moderate({bbw:.4f}) +1'
        else:
            score += 0; details['bb_width'] = f'tight({bbw:.4f}) +0'
        
        # Quality label
        if score >= 10:
            label = 'ELITE'
        elif score >= 7:
            label = 'SOLID'
        elif score >= 4:
            label = 'OK'
        else:
            label = 'WEAK'
        
        return {
            'score': score,
            'max': 14,
            'label': label,
            'details': details,
        }

    def _get_latest_candle_pattern(self, df):
        """Helper method to extract the latest candlestick pattern for profile matching."""
        if len(df) < 1:
            return 'unknown'

        latest_row = df.iloc[-1]

        # Check for the most common patterns detected by the pattern detection
        pattern_columns = [col for col in df.columns if 'pattern' in col.lower() or col in [
            'hammer', 'doji', 'engulfing_bull', 'engulfing_bear', 'morning_star', 'evening_star',
            'shooting_star', 'spinning_top', 'marubozu_bull', 'marubozu_bear'
        ]]

        for col in pattern_columns:
            if col in latest_row and latest_row[col] == 1:
                return col

        # Fallback to basic pattern detection
        open_price = latest_row.get('open', 0)
        close_price = latest_row.get('close', 0)
        high_price = latest_row.get('high', 0)
        low_price = latest_row.get('low', 0)

        body_size = abs(close_price - open_price)
        total_range = high_price - low_price

        if total_range == 0:
            return 'doji'

        if body_size / total_range < 0.1:
            return 'doji'
        elif close_price > open_price:
            return 'bullish'
        else:
            return 'bearish'


if __name__ == "__main__":
    import sys
    import signal as _signal
    import atexit

    # ── PID file guard: kill stale scout before starting ──────────────────
    _SCOUT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PID_DIR = os.path.join(_SCOUT_DIR, '.pids')
    os.makedirs(_PID_DIR, exist_ok=True)
    _PID_FILE = os.path.join(_PID_DIR, 'scout.pid')

    if os.path.exists(_PID_FILE):
        try:
            _old_pid = int(open(_PID_FILE).read().strip())
            _my_pid = os.getpid()
            _my_ppid = os.getppid()

            # Safety: never kill ourselves or our parent bash wrapper.
            # trading_launcher.sh uses `nohup bash -c "$cmd"` which writes
            # the bash wrapper PID to this same file. If we kill that PID,
            # we kill our own parent process → instant death.
            if _old_pid == _my_pid or _old_pid == _my_ppid:
                print(f"[SCOUT] PID {_old_pid} is self or parent — skipping kill")
            else:
                os.kill(_old_pid, 0)  # Check if alive
                print(f"[SCOUT] Killing stale scout process (PID {_old_pid})")
                os.kill(_old_pid, _signal.SIGTERM)
                import time as _t; _t.sleep(2)
                try:
                    os.kill(_old_pid, _signal.SIGKILL)
                except ProcessLookupError:
                    pass
        except (ProcessLookupError, ValueError, OSError):
            pass  # Already dead or invalid PID

    with open(_PID_FILE, 'w') as _pf:
        _pf.write(str(os.getpid()))

    def _cleanup_pid():
        try:
            os.remove(_PID_FILE)
        except OSError:
            pass
    atexit.register(_cleanup_pid)
    # ─────────────────────────────────────────────────────────────────────

    # Setup logging — use absolute path so the log file lands in the right place
    # regardless of cwd, and force=True so this wins even if a prior import
    # already called basicConfig.
    _log_file = os.path.join(_SCOUT_DIR, 'logs', 'scout.log')
    os.makedirs(os.path.dirname(_log_file), exist_ok=True)

    # Detect if stdout is redirected (nohup / trading_launcher.sh).
    # When stdout → file, adding StreamHandler(stdout) duplicates every line
    # because FileHandler writes to scout.log AND stdout also goes to scout.log.
    _handlers = [logging.FileHandler(_log_file)]
    if sys.stdout.isatty():
        # Interactive terminal — add console output
        _handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=_handlers,
        force=True,
    )
    # Suppress websockets connection-level noise (400s from browser reconnects / health probes)
    logging.getLogger("websockets.server").setLevel(logging.WARNING)

    # Create and start the trade scout
    scout = TradeScout()

    # ── Clean shutdown on SIGTERM / SIGINT ────────────────────────────────
    # Without this, watchdog SIGTERM or systemd stop kills the process abruptly.
    # scout.stop() sets self.running=False so loops exit cleanly.
    # The guardian re-discovers open trades on next reconcile — no positions lost.
    def _handle_signal(sig, frame):
        logger.info("[SCOUT] Signal %s received — initiating clean shutdown", sig)
        scout.stop()
        # Give the event loop a chance to drain — use stored loop ref (not asyncio.get_event_loop()
        # which fails in thread context with "no current event loop in thread")
        import threading as _thr
        def _stop_loop():
            import time as _t; _t.sleep(2)
            loop = getattr(scout, '_loop', None)
            if loop and loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
        _thr.Thread(target=_stop_loop, daemon=True).start()

    _signal.signal(_signal.SIGTERM, _handle_signal)
    _signal.signal(_signal.SIGINT,  _handle_signal)

    try:
        asyncio.run(scout.start())
    except (KeyboardInterrupt, RuntimeError):
        # RuntimeError raised when loop.stop() is called by signal handler
        pass
    finally:
        logger.info("[SCOUT] Clean shutdown complete")
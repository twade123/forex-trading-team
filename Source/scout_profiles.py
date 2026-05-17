#!/usr/bin/env python3
"""
Scout Condition Fingerprint System with Learning Loop

Pre-computes condition profiles from 8.5M backtest trades and provides fast pattern matching
for trade scout with continuous learning from live findings.

Features:
- Pre-computed profile cache from backtest_trades data
- Fast pattern matching (<5ms per call)
- Relaxed matching when exact patterns not found
- Learning loop that tracks outcomes and adjusts confidence
- Self-contained module with no trade_scout imports

Performance Requirements:
- Handles 8.5M records efficiently via per-pair processing
- Profile cache uses ~50-100MB memory
- Match function optimized for real-time scanning
"""

import os
import sys
import sqlite3
import logging
import time
import psutil
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict
import json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ProfileStats:
    """Statistics for a specific condition profile."""
    def __init__(self):
        self.trade_count: int = 0
        self.win_count: int = 0
        self.win_rate: float = 0.0
        self.avg_pips: float = 0.0
        self.avg_mfe: float = 0.0  # Max favorable excursion
        self.avg_mae: float = 0.0  # Max adverse excursion
        self.avg_candles_to_exit: float = 0.0
        self.best_rr_mult: float = 0.0
        self.best_sl_mult: float = 0.0

class LiveStats:
    """Live statistics for continuous learning."""
    def __init__(self):
        self.total_findings: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.neutral: int = 0
        self.live_win_rate: float = 0.0
        self.avg_pips: float = 0.0
        self.last_updated: str = ""
        self.last_7d_wins: int = 0
        self.last_7d_total: int = 0
        self.last_7d_win_rate: float = 0.0

class ScoutProfileEngine:
    """
    Pre-computed condition fingerprint system for fast trading signal matching.
    """
    
    def __init__(self, db_path: Optional[str] = None):
        """Initialize the profile engine.
        
        Args:
            db_path: Path to v2/trading_forex.db. Defaults to standard location.
        """
        if db_path is None:
            db_path = "~/jarvis/Database/v2/trading_forex.db"
        
        self.db_path = db_path
        # Separate clean DB for profile cache — avoids trevor_database.db corruption issues
        self.cache_db_path = "~/jarvis/Database/scout_profile_cache.db"
        self.profiles: Dict[Tuple, ProfileStats] = {}  # {profile_key: ProfileStats}
        self.live_results: Dict[Tuple, LiveStats] = {}  # {profile_key: LiveStats}
        
        # Verify database exists
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(f"Database not found: {self.db_path}")
        
        logger.info(f"Initializing Scout Profile Engine with database: {self.db_path}")
        self._ready = False  # set True when profiles are loaded

        # Create necessary tables
        self._create_tables()

        # Only rebuild if backtest data changed since last build
        if self._profiles_are_stale():
            self._build_profiles()
        else:
            self._load_cached_profiles()

        # Load existing live stats
        self._load_live_stats()

        self._ready = True
        logger.info(f"Scout Profile Engine ready with {len(self.profiles):,} profiles")

    def _backtest_table_exists(self) -> bool:
        """Return True if backtest_trades table exists in the main DB."""
        try:
            with sqlite3.connect(self.db_path, isolation_level=None) as conn:
                conn.execute("SELECT 1 FROM backtest_trades LIMIT 1")
            return True
        except sqlite3.OperationalError:
            return False

    def _profiles_are_stale(self) -> bool:
        """Check if backtest_trades has changed since profiles were last built."""
        try:
            # Read backtest fingerprint from main DB (read-only)
            with sqlite3.connect(self.db_path, isolation_level=None) as conn:
                cur = conn.execute("SELECT COUNT(*), MAX(rowid) FROM backtest_trades")
                row = cur.fetchone()
                current_sig = f"{row[0]}:{row[1]}"

            # Read stored signature from clean cache DB (not the corrupted main DB)
            with sqlite3.connect(self.cache_db_path, isolation_level=None) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS scout_profile_meta (key TEXT PRIMARY KEY, value TEXT)")
                cur2 = conn.execute(
                    "SELECT value FROM scout_profile_meta WHERE key='backtest_signature'"
                )
                stored = cur2.fetchone()
                if stored and stored[0] == current_sig:
                    logger.info(f"[profiles] Backtest data unchanged ({current_sig}) — loading from cache")
                    return False
                logger.info(f"[profiles] Backtest data changed or first run — rebuilding profiles")
                return True
        except Exception as e:
            if "no such table" in str(e).lower():
                logger.warning("[profiles] backtest_trades table not found — no profile data to build from, skipping")
                return False  # nothing to rebuild; load from cache instead
            return True  # Rebuild on other errors

    def _save_profile_signature(self):
        """Save current backtest fingerprint AND profile data after a successful build."""
        import json as _json
        try:
            # Use cache_db_path (clean file) — avoids trevor_database.db corruption
            with sqlite3.connect(self.cache_db_path, isolation_level=None) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS scout_profile_meta (key TEXT PRIMARY KEY, value TEXT)")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS scout_profile_cache (
                        profile_key TEXT PRIMARY KEY,
                        win_rate REAL DEFAULT 0,
                        trade_count INTEGER DEFAULT 0,
                        avg_pips REAL DEFAULT 0
                    )
                """)
                # Get backtest fingerprint from main DB
                with sqlite3.connect(self.db_path, isolation_level=None) as main_conn:
                    cur = main_conn.execute("SELECT COUNT(*), MAX(rowid) FROM backtest_trades")
                    row = cur.fetchone()
                    sig = f"{row[0]}:{row[1]}"
                conn.execute(
                    "INSERT OR REPLACE INTO scout_profile_meta (key, value) VALUES ('backtest_signature', ?)",
                    (sig,)
                )
                # Save all built profiles to cache table
                conn.execute("DELETE FROM scout_profile_cache")
                rows = [
                    (_json.dumps(list(k)), ps.win_rate, ps.trade_count, ps.avg_pips)
                    for k, ps in self.profiles.items()
                ]
                conn.executemany(
                    "INSERT OR REPLACE INTO scout_profile_cache (profile_key, win_rate, trade_count, avg_pips) VALUES (?,?,?,?)",
                    rows
                )
                conn.commit()
                logger.info(f"[profiles] Saved {len(rows):,} profiles to cache ({self.cache_db_path})")
        except Exception as e:
            logger.warning(f"[profiles] Could not save signature: {e}")

    def _load_cached_profiles(self):
        """Load pre-built profiles from scout_profile_cache instead of recomputing."""
        import json as _json
        logger.info("[profiles] Loading cached profiles from DB...")
        start = __import__('time').time()
        loaded = 0
        try:
            with sqlite3.connect(self.cache_db_path, isolation_level=None) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute("SELECT profile_key, win_rate, trade_count, avg_pips FROM scout_profile_cache")
                for row in cur.fetchall():
                    try:
                        key = tuple(_json.loads(row['profile_key']))
                        ps = ProfileStats()
                        ps.win_rate = row['win_rate']
                        ps.trade_count = row['trade_count']
                        ps.avg_pips = row['avg_pips']
                        self.profiles[key] = ps
                        loaded += 1
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[profiles] Cache load failed ({e}) — rebuilding")
            if not self._backtest_table_exists():
                logger.warning("[profiles] backtest_trades missing — starting with 0 profiles")
                return
            self._build_profiles()
            return
        if loaded == 0:
            logger.warning("[profiles] Cache was empty — rebuilding")
            if not self._backtest_table_exists():
                logger.warning("[profiles] backtest_trades missing — starting with 0 profiles")
                return
            self._build_profiles()
            return
        elapsed = __import__('time').time() - start
        logger.info(f"[profiles] Loaded {loaded:,} cached profiles in {elapsed:.1f}s")

    def _create_tables(self):
        """Create necessary database tables if they don't exist."""
        with sqlite3.connect(self.db_path, isolation_level=None) as conn:
            # Scout profile findings table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scout_profile_findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    profile_key TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    rsi REAL,
                    stoch_k REAL,
                    regime TEXT,
                    session TEXT,
                    candle_pattern TEXT,
                    bb_zone TEXT,
                    ema_fan_state TEXT,
                    ema_velocity REAL,
                    ema_trend_health INTEGER,
                    session_quality REAL,
                    historical_confidence REAL,
                    blended_confidence REAL,
                    match_quality TEXT,
                    -- Outcome fields (filled later)
                    outcome TEXT,  -- 'win', 'loss', 'neutral', NULL=pending
                    pips_1h REAL,  -- Price movement after 1 hour
                    pips_4h REAL,  -- Price movement after 4 hours
                    was_traded BOOLEAN DEFAULT 0,
                    trade_pips REAL,  -- Actual trade result if traded
                    resolved_at TEXT
                )
            """)
            
            # Meta table for build fingerprinting (skip rebuild when data unchanged)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scout_profile_meta
                (key TEXT PRIMARY KEY, value TEXT)
            """)

            # Profile cache table — serialized ProfileStats for fast load
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scout_profile_cache (
                    profile_key TEXT PRIMARY KEY,
                    win_rate REAL DEFAULT 0,
                    trade_count INTEGER DEFAULT 0,
                    avg_pips REAL DEFAULT 0
                )
            """)

            # Scout profile live stats table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scout_profile_live_stats (
                    profile_key TEXT PRIMARY KEY,
                    pair TEXT NOT NULL,
                    total_findings INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    neutral INTEGER DEFAULT 0,
                    live_win_rate REAL DEFAULT 0,
                    avg_pips REAL DEFAULT 0,
                    last_updated TEXT,
                    last_7d_wins INTEGER DEFAULT 0,
                    last_7d_total INTEGER DEFAULT 0,
                    last_7d_win_rate REAL DEFAULT 0
                )
            """)
            
            conn.commit()

    def _build_profiles(self):
        """Pre-compute profiles from backtest_trades. Runs once on startup."""
        logger.info("Building condition profiles from backtest data...")
        start_time = time.time()
        
        # Get all unique pairs for per-pair processing
        with sqlite3.connect(self.db_path, isolation_level=None) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT pair FROM backtest_trades")
            pairs = [row[0] for row in cursor.fetchall()]
        
        logger.info(f"Processing {len(pairs)} pairs from backtest_trades...")
        
        total_profiles = 0
        for pair in pairs:
            pair_start = time.time()
            pair_profiles = self._build_profiles_for_pair(pair)
            pair_time = time.time() - pair_start
            total_profiles += pair_profiles
            logger.info(f"  {pair}: {pair_profiles:,} profiles in {pair_time:.1f}s")
        
        total_time = time.time() - start_time
        logger.info(f"Profile building complete: {total_profiles:,} profiles in {total_time:.1f}s")
        self._save_profile_signature()

    def _build_profiles_for_pair(self, pair: str) -> int:
        """Build profiles for a specific pair to manage memory efficiently."""
        with sqlite3.connect(self.db_path, isolation_level=None) as conn:
            # Query trades for this pair with all needed indicator data
            query = """
                SELECT 
                    pair, direction, regime, rsi, stoch_k, stoch_d, session,
                    entry_candle_pattern, bb_upper, bb_mid, bb_lower, entry_price,
                    result, pips, max_favorable_pips, max_adverse_pips,
                    candles_to_exit, rr_mult, sl_mult
                FROM backtest_trades 
                WHERE pair = ?
                AND rsi IS NOT NULL 
                AND stoch_k IS NOT NULL
                AND entry_candle_pattern IS NOT NULL
                AND bb_upper IS NOT NULL
                AND bb_mid IS NOT NULL  
                AND bb_lower IS NOT NULL
                AND pips IS NOT NULL
            """
            
            cursor = conn.cursor()
            cursor.execute(query, (pair,))
            
            # Process trades in batches to manage memory
            batch_size = 100000
            pair_stats = defaultdict(lambda: {
                'trades': 0, 'wins': 0, 'total_pips': 0,
                'total_mfe': 0, 'total_mae': 0, 'total_candles': 0,
                'rr_mults': [], 'sl_mults': []
            })
            
            while True:
                rows = cursor.fetchmany(batch_size)
                if not rows:
                    break
                
                for row in rows:
                    (pair, direction, regime, rsi, stoch_k, stoch_d, session,
                     candle_pattern, bb_upper, bb_mid, bb_lower, entry_price,
                     result, pips, max_favorable_pips, max_adverse_pips,
                     candles_to_exit, rr_mult, sl_mult) = row
                    
                    # Classify indicators into zones
                    rsi_zone = self._rsi_zone(rsi)
                    stoch_zone = self._stoch_zone(stoch_k)
                    bb_zone = self._bb_zone_from_price(entry_price, bb_upper, bb_mid, bb_lower)
                    candle_bucket = self._candle_bucket(candle_pattern)
                    
                    # Create profile key
                    profile_key = (pair, direction, regime, rsi_zone, stoch_zone, 
                                 session, candle_bucket, bb_zone)
                    
                    # Accumulate statistics
                    stats = pair_stats[profile_key]
                    stats['trades'] += 1
                    if result == 'win':
                        stats['wins'] += 1
                    
                    # Handle None values safely
                    if pips is not None:
                        stats['total_pips'] += pips
                    if max_favorable_pips is not None:
                        stats['total_mfe'] += max_favorable_pips
                    if max_adverse_pips is not None:
                        stats['total_mae'] += max_adverse_pips
                    if candles_to_exit is not None:
                        stats['total_candles'] += candles_to_exit
                    if rr_mult is not None:
                        stats['rr_mults'].append(rr_mult)
                    if sl_mult is not None:
                        stats['sl_mults'].append(sl_mult)
        
        # Convert accumulated stats to ProfileStats objects (only keep profiles with >= 20 trades)
        created_profiles = 0
        for profile_key, stats in pair_stats.items():
            if stats['trades'] >= 20:  # Minimum threshold for statistical significance
                profile_stats = ProfileStats()
                profile_stats.trade_count = stats['trades']
                profile_stats.win_count = stats['wins']
                profile_stats.win_rate = (stats['wins'] / stats['trades']) * 100
                profile_stats.avg_pips = stats['total_pips'] / stats['trades'] if stats['trades'] > 0 else 0
                profile_stats.avg_mfe = stats['total_mfe'] / stats['trades'] if stats['trades'] > 0 else 0
                profile_stats.avg_mae = stats['total_mae'] / stats['trades'] if stats['trades'] > 0 else 0
                profile_stats.avg_candles_to_exit = stats['total_candles'] / stats['trades'] if stats['trades'] > 0 else 0
                
                # Best multipliers (most common values)
                if stats['rr_mults']:
                    profile_stats.best_rr_mult = max(set(stats['rr_mults']), key=stats['rr_mults'].count)
                if stats['sl_mults']:
                    profile_stats.best_sl_mult = max(set(stats['sl_mults']), key=stats['sl_mults'].count)
                
                self.profiles[profile_key] = profile_stats
                created_profiles += 1
        
        return created_profiles

    def _load_live_stats(self):
        """Load existing live statistics from database."""
        with sqlite3.connect(self.db_path, isolation_level=None) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM scout_profile_live_stats")
            
            for row in cursor.fetchall():
                profile_key_str = row[0]  # Convert back from string
                profile_key = tuple(profile_key_str.split('|'))  # Simple string split
                
                stats = LiveStats()
                stats.total_findings = row[2]
                stats.wins = row[3]
                stats.losses = row[4]
                stats.neutral = row[5]
                stats.live_win_rate = row[6]
                stats.avg_pips = row[7]
                stats.last_updated = row[8]
                stats.last_7d_wins = row[9]
                stats.last_7d_total = row[10]
                stats.last_7d_win_rate = row[11]
                
                self.live_results[profile_key] = stats

    @staticmethod
    def _rsi_zone(rsi: float) -> str:
        """Classify RSI into zones."""
        if rsi < 20:
            return "extreme_oversold"
        elif rsi < 30:
            return "oversold"
        elif rsi < 45:
            return "neutral_low"
        elif rsi < 55:
            return "neutral"
        elif rsi < 70:
            return "neutral_high"
        elif rsi < 80:
            return "overbought"
        else:
            return "extreme_overbought"

    @staticmethod
    def _stoch_zone(stoch_k: float) -> str:
        """Classify Stochastic K into zones."""
        if stoch_k < 20:
            return "oversold"
        elif stoch_k < 50:
            return "neutral_low"
        elif stoch_k < 80:
            return "neutral_high"
        else:
            return "overbought"

    @staticmethod
    def _bb_zone_from_price(price: float, bb_upper: float, bb_mid: float, bb_lower: float) -> str:
        """Classify Bollinger Band position from price and band values."""
        if price < bb_lower:
            return "below_lower"
        elif price < bb_mid:
            return "lower_to_mid"
        elif price < bb_upper:
            return "mid_to_upper"
        else:
            return "above_upper"

    @staticmethod
    def _bb_zone(bb_position_str_or_price, bb_upper=None, bb_lower=None) -> str:
        """Classify Bollinger Band position - supports both string and price inputs."""
        # Handle list/tuple inputs (take first element)
        if isinstance(bb_position_str_or_price, (list, tuple)):
            bb_position_str_or_price = bb_position_str_or_price[0] if bb_position_str_or_price else "unknown"
        # For backward compatibility with string inputs
        if isinstance(bb_position_str_or_price, str):
            return bb_position_str_or_price
        
        # Price-based calculation (requires bb_upper and bb_lower)
        if bb_upper is not None and bb_lower is not None:
            price = bb_position_str_or_price
            bb_mid = (bb_upper + bb_lower) / 2
            return ScoutProfileEngine._bb_zone_from_price(price, bb_upper, bb_mid, bb_lower)
        
        return "unknown"

    @staticmethod
    def _candle_bucket(pattern: str) -> str:
        """Group candle patterns into categories."""
        if pattern is None or pattern == "none":
            return "none"
        
        pattern = pattern.lower()
        
        # Reversal patterns
        reversal_patterns = [
            'hammer', 'inverted_hammer', 'hanging_man', 'shooting_star',
            'bullish_engulfing', 'bearish_engulfing', 'morning_star', 'evening_star',
            'piercing', 'dark_cloud', 'harami', 'harami_cross'
        ]
        
        # Strong directional patterns
        strong_patterns = ['marubozu', 'long_white', 'long_black']
        
        # Continuation patterns
        continuation_patterns = ['doji', 'spinning_top']
        
        if any(rev in pattern for rev in reversal_patterns):
            return "reversal"
        elif any(strong in pattern for strong in strong_patterns):
            return "strong"
        elif any(cont in pattern for cont in continuation_patterns):
            return "continuation"
        else:
            return "other"

    def match(self, pair: str, direction: str, regime: str, rsi: float, stoch_k: float, 
             session: str, candle_pattern: str, bb_position) -> Dict[str, Any]:
        """
        Match current conditions against pre-computed profiles.
        
        Returns a comprehensive match result with confidence scoring and suggestions.
        """
        # Classify current conditions
        rsi_zone = self._rsi_zone(rsi)
        stoch_zone = self._stoch_zone(stoch_k)
        bb_zone = self._bb_zone(bb_position)
        candle_bucket = self._candle_bucket(candle_pattern)
        
        # Try exact match first
        exact_key = (pair, direction, regime, rsi_zone, stoch_zone, session, candle_bucket, bb_zone)
        
        if exact_key in self.profiles:
            return self._build_match_result(exact_key, "exact")
        
        # Relaxed matching: progressively drop dimensions
        relaxed_matches = [
            # Drop candle pattern
            (pair, direction, regime, rsi_zone, stoch_zone, session, "any", bb_zone),
            # Drop session
            (pair, direction, regime, rsi_zone, stoch_zone, "any", "any", bb_zone),
            # Drop regime (broadest match)
            (pair, direction, "any", rsi_zone, stoch_zone, "any", "any", bb_zone)
        ]
        
        for i, relaxed_key in enumerate(relaxed_matches, 1):
            # Find best match for this relaxed pattern
            best_match = self._find_best_relaxed_match(relaxed_key)
            if best_match:
                return self._build_match_result(best_match, f"relaxed_{i}")
        
        # No match found - return default response
        return {
            'confidence': 0.0,
            'historical_win_rate': 0.0,
            'historical_trades': 0,
            'live_win_rate': None,
            'live_trades': 0,
            'blend_ratio': 0.0,
            'avg_pips': 0.0,
            'avg_mfe': 0.0,
            'avg_mae': 0.0,
            'match_quality': 'no_match',
            'profile_key': str(exact_key),
            'suggested_tp_pips': 15.0,  # Default conservative target
            'suggested_sl_pips': 10.0   # Default conservative stop
        }

    def _find_best_relaxed_match(self, relaxed_key: Tuple) -> Optional[Tuple]:
        """Find the best matching profile for a relaxed key pattern."""
        candidates = []
        
        for profile_key in self.profiles.keys():
            if self._matches_relaxed_pattern(profile_key, relaxed_key):
                profile_stats = self.profiles[profile_key]
                # Score by win rate * trade count (reliability * significance)
                score = profile_stats.win_rate * min(profile_stats.trade_count / 100, 1.0)
                candidates.append((profile_key, score))
        
        if candidates:
            # Return the highest scoring candidate
            return max(candidates, key=lambda x: x[1])[0]
        
        return None

    def _matches_relaxed_pattern(self, profile_key: Tuple, relaxed_key: Tuple) -> bool:
        """Check if a profile key matches a relaxed pattern."""
        for i, (profile_val, relaxed_val) in enumerate(zip(profile_key, relaxed_key)):
            if relaxed_val != "any" and profile_val != relaxed_val:
                return False
        return True

    def _build_match_result(self, profile_key: Tuple, match_quality: str) -> Dict[str, Any]:
        """Build a complete match result from a profile key."""
        historical_stats = self.profiles[profile_key]
        live_stats = self.live_results.get(profile_key)
        
        # Calculate blended confidence
        blended_confidence = self._blend_confidence(profile_key)
        
        # Calculate suggested targets based on historical performance
        suggested_tp = max(historical_stats.avg_mfe * 0.7, 10.0) if historical_stats.avg_mfe > 0 else 15.0
        suggested_sl = max(historical_stats.avg_mae * 1.2, 8.0) if historical_stats.avg_mae > 0 else 10.0
        
        # Blend ratio calculation
        if live_stats and live_stats.total_findings >= 30:
            if live_stats.total_findings >= 500:
                blend_ratio = 0.7  # 70% live data
            elif live_stats.total_findings >= 100:
                blend_ratio = 0.5  # 50% live data
            else:
                blend_ratio = 0.3  # 30% live data
        else:
            blend_ratio = 0.0  # 100% historical
        
        return {
            'confidence': blended_confidence,
            'historical_win_rate': historical_stats.win_rate,
            'historical_trades': historical_stats.trade_count,
            'live_win_rate': live_stats.live_win_rate if live_stats else None,
            'live_trades': live_stats.total_findings if live_stats else 0,
            'blend_ratio': blend_ratio,
            'avg_pips': historical_stats.avg_pips,
            'avg_mfe': historical_stats.avg_mfe,
            'avg_mae': historical_stats.avg_mae,
            'match_quality': match_quality,
            'profile_key': str(profile_key),
            'suggested_tp_pips': suggested_tp,
            'suggested_sl_pips': suggested_sl
        }

    def _blend_confidence(self, profile_key: Tuple) -> float:
        """Blend historical and live data for confidence scoring."""
        historical_stats = self.profiles[profile_key]
        live_stats = self.live_results.get(profile_key)
        
        # Base historical confidence (win rate adjusted for trade count)
        base_confidence = historical_stats.win_rate / 100.0
        trade_count_factor = min(historical_stats.trade_count / 1000.0, 1.0)  # Max factor of 1.0 at 1000+ trades
        historical_confidence = base_confidence * (0.5 + 0.5 * trade_count_factor)
        
        if not live_stats or live_stats.total_findings < 30:
            # Not enough live data - use historical only
            return historical_confidence
        
        # Calculate live confidence with recency bias
        live_confidence = live_stats.live_win_rate / 100.0 if live_stats.live_win_rate > 0 else 0.0
        
        # Apply recency weight (last 7 days count 2x)
        if live_stats.last_7d_total > 0:
            recent_boost = (live_stats.last_7d_win_rate / 100.0) * 0.1  # 10% boost for recent performance
            live_confidence = min(live_confidence + recent_boost, 1.0)
        
        # Blend based on live data quantity
        if live_stats.total_findings >= 500:
            return historical_confidence * 0.3 + live_confidence * 0.7
        elif live_stats.total_findings >= 100:
            return historical_confidence * 0.5 + live_confidence * 0.5
        else:  # 30-100 findings
            return historical_confidence * 0.7 + live_confidence * 0.3

    def record_finding(self, profile_key: str, entry_price: float, pair: str, timestamp: str = None,
                      **additional_data) -> int:
        """
        Record when scout flags an opportunity for outcome tracking.
        
        Returns the finding_id for later outcome updates.
        """
        if timestamp is None:
            timestamp = datetime.now().isoformat()
        
        with sqlite3.connect(self.db_path, isolation_level=None) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO scout_profile_findings (
                    timestamp, profile_key, pair, direction, entry_price,
                    rsi, stoch_k, regime, session, candle_pattern, bb_zone,
                    ema_fan_state, ema_velocity, ema_trend_health, session_quality,
                    historical_confidence, blended_confidence, match_quality
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp, profile_key, pair, 
                additional_data.get('direction', ''),
                entry_price,
                additional_data.get('rsi'),
                additional_data.get('stoch_k'), 
                additional_data.get('regime'),
                additional_data.get('session'),
                additional_data.get('candle_pattern'),
                additional_data.get('bb_zone'),
                additional_data.get('ema_fan_state'),
                additional_data.get('ema_velocity'),
                additional_data.get('ema_trend_health'),
                additional_data.get('session_quality'),
                additional_data.get('historical_confidence'),
                additional_data.get('blended_confidence'),
                additional_data.get('match_quality')
            ))
            
            finding_id = cursor.lastrowid
            conn.commit()
            return finding_id

    def update_outcome(self, finding_id: int, exit_price: float, pips_result: float, 
                      was_traded: bool = False):
        """Update a finding with its outcome and update live statistics."""
        with sqlite3.connect(self.db_path, isolation_level=None) as conn:
            cursor = conn.cursor()
            
            # Get the finding details
            cursor.execute("SELECT profile_key, pair FROM scout_profile_findings WHERE id = ?", (finding_id,))
            result = cursor.fetchone()
            if not result:
                logger.warning(f"Finding {finding_id} not found")
                return
            
            profile_key_str, pair = result
            
            # Determine outcome
            if pips_result > 10:
                outcome = "win"
            elif pips_result < -10:
                outcome = "loss"
            else:
                outcome = "neutral"
            
            # Update the finding
            cursor.execute("""
                UPDATE scout_profile_findings 
                SET outcome = ?, trade_pips = ?, was_traded = ?, resolved_at = ?
                WHERE id = ?
            """, (outcome, pips_result, was_traded, datetime.now().isoformat(), finding_id))
            
            # Update live statistics
            self._update_live_stats(profile_key_str, outcome, pips_result, conn)
            
            conn.commit()

    def _update_live_stats(self, profile_key_str: str, outcome: str, pips_result: float, conn):
        """Update live statistics for a profile."""
        cursor = conn.cursor()
        
        # Get current stats
        cursor.execute("SELECT * FROM scout_profile_live_stats WHERE profile_key = ?", (profile_key_str,))
        current = cursor.fetchone()
        
        if current:
            # Update existing stats
            total_findings = current[2] + 1
            wins = current[3] + (1 if outcome == "win" else 0)
            losses = current[4] + (1 if outcome == "loss" else 0)
            neutral = current[5] + (1 if outcome == "neutral" else 0)
            
            live_win_rate = (wins / total_findings) * 100 if total_findings > 0 else 0
            
            # Simple moving average for pips
            current_avg_pips = current[7]
            new_avg_pips = ((current_avg_pips * (total_findings - 1)) + pips_result) / total_findings
            
            cursor.execute("""
                UPDATE scout_profile_live_stats 
                SET total_findings = ?, wins = ?, losses = ?, neutral = ?,
                    live_win_rate = ?, avg_pips = ?, last_updated = ?
                WHERE profile_key = ?
            """, (total_findings, wins, losses, neutral, live_win_rate, new_avg_pips,
                  datetime.now().isoformat(), profile_key_str))
        
        else:
            # Create new stats entry
            profile_key_tuple = tuple(profile_key_str.split('|'))
            pair = profile_key_tuple[0] if profile_key_tuple else "UNKNOWN"
            
            wins = 1 if outcome == "win" else 0
            losses = 1 if outcome == "loss" else 0
            neutral = 1 if outcome == "neutral" else 0
            live_win_rate = wins * 100  # First entry
            
            cursor.execute("""
                INSERT INTO scout_profile_live_stats (
                    profile_key, pair, total_findings, wins, losses, neutral,
                    live_win_rate, avg_pips, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (profile_key_str, pair, 1, wins, losses, neutral,
                  live_win_rate, pips_result, datetime.now().isoformat()))

    def resolve_pending_findings(self):
        """
        Check old unresolved findings and resolve based on price movement.
        Run periodically (every 30 min).
        """
        logger.info("Resolving pending findings...")
        
        # This would require price data access to check 1h and 4h movements
        # For now, implement a placeholder that logs the intent
        with sqlite3.connect(self.db_path, isolation_level=None) as conn:
            cursor = conn.cursor()
            
            # Get findings older than 4 hours that are unresolved
            cutoff_time = (datetime.now() - timedelta(hours=4)).isoformat()
            cursor.execute("""
                SELECT id, pair, direction, entry_price, timestamp 
                FROM scout_profile_findings 
                WHERE outcome IS NULL AND timestamp < ?
                LIMIT 100
            """, (cutoff_time,))
            
            pending = cursor.fetchall()
            logger.info(f"Found {len(pending)} pending findings to resolve")
            
            # TODO: Implement actual price movement checking with historical data
            # For now, mark as neutral (would need market data access)
            for finding_id, pair, direction, entry_price, timestamp in pending:
                self.update_outcome(finding_id, entry_price, 0.0, was_traded=False)

    def rebuild_daily(self):
        """
        Rebuild profiles incorporating live learning data.
        Run once per day (or on restart).
        """
        logger.info("Starting daily profile rebuild...")
        
        # Clear current profiles
        old_count = len(self.profiles)
        self.profiles.clear()
        
        # Rebuild from backtest data
        self._build_profiles()
        
        # Reload live stats
        self._load_live_stats()
        
        new_count = len(self.profiles)
        logger.info(f"Daily rebuild complete: {old_count:,} -> {new_count:,} profiles")

    def get_memory_usage(self) -> Dict[str, Any]:
        """Report current memory usage of the profile cache."""
        process = psutil.Process()
        memory_info = process.memory_info()
        
        return {
            'total_profiles': len(self.profiles),
            'total_live_stats': len(self.live_results),
            'memory_mb': memory_info.rss / 1024 / 1024,
            'memory_percent': process.memory_percent()
        }

    def get_top_profiles(self, pair: str = None, limit: int = 20) -> List[Dict[str, Any]]:
        """Get top performing profiles, optionally filtered by pair."""
        profiles = []
        
        for profile_key, stats in self.profiles.items():
            if pair and profile_key[0] != pair:
                continue
            
            # Calculate confidence score
            confidence = self._blend_confidence(profile_key)
            
            profiles.append({
                'pair': profile_key[0],
                'direction': profile_key[1],
                'regime': profile_key[2],
                'rsi_zone': profile_key[3],
                'stoch_zone': profile_key[4],
                'session': profile_key[5],
                'candle_bucket': profile_key[6],
                'bb_zone': profile_key[7],
                'win_rate': stats.win_rate,
                'trade_count': stats.trade_count,
                'avg_pips': stats.avg_pips,
                'confidence': confidence,
                'profile_key': str(profile_key)
            })
        
        # Sort by confidence score (blended historical + live performance)
        profiles.sort(key=lambda x: x['confidence'], reverse=True)
        return profiles[:limit]


if __name__ == "__main__":
    """
    Test harness and demonstration of the Scout Profile Engine.
    """
    print("🎯 Scout Condition Fingerprint System")
    print("=" * 60)
    
    try:
        # Initialize the engine
        print("Initializing Scout Profile Engine...")
        engine = ScoutProfileEngine()
        
        # Report stats
        memory_usage = engine.get_memory_usage()
        print(f"\n📊 System Stats:")
        print(f"  Total profiles: {memory_usage['total_profiles']:,}")
        print(f"  Live stats: {memory_usage['total_live_stats']:,}")
        print(f"  Memory usage: {memory_usage['memory_mb']:.1f} MB")
        
        # Show profile breakdown per pair
        pair_counts = defaultdict(int)
        for profile_key in engine.profiles.keys():
            pair_counts[profile_key[0]] += 1
        
        print(f"\n📈 Profiles by Pair:")
        for pair, count in sorted(pair_counts.items()):
            print(f"  {pair}: {count:,} profiles")
        
        # Test matching with sample conditions
        print(f"\n🔍 Testing Match Function:")
        test_conditions = [
            {
                'pair': 'EUR_USD',
                'direction': 'buy',
                'regime': 'trending_up',
                'rsi': 65.0,
                'stoch_k': 75.0,
                'session': 'London',
                'candle_pattern': 'bullish_engulfing',
                'bb_position': 'mid_to_upper'
            },
            {
                'pair': 'GBP_JPY',
                'direction': 'sell',
                'regime': 'ranging',
                'rsi': 35.0,
                'stoch_k': 25.0,
                'session': 'NY_Overlap',
                'candle_pattern': 'doji',
                'bb_position': 'lower_to_mid'
            }
        ]
        
        for i, conditions in enumerate(test_conditions, 1):
            print(f"\n  Test {i}: {conditions['pair']} {conditions['direction']}")
            start_time = time.time()
            match = engine.match(**conditions)
            match_time = (time.time() - start_time) * 1000  # Convert to milliseconds
            
            print(f"    Match quality: {match['match_quality']}")
            print(f"    Confidence: {match['confidence']:.2f}")
            print(f"    Historical: {match['historical_win_rate']:.1f}% ({match['historical_trades']:,} trades)")
            print(f"    Suggested TP: {match['suggested_tp_pips']:.1f} pips")
            print(f"    Suggested SL: {match['suggested_sl_pips']:.1f} pips")
            print(f"    Match time: {match_time:.2f}ms")
        
        # Show top profiles per pair (sample)
        sample_pairs = ['EUR_USD', 'GBP_JPY', 'USD_JPY']
        print(f"\n🏆 Top 5 Profiles by Pair:")
        for pair in sample_pairs:
            top_profiles = engine.get_top_profiles(pair=pair, limit=5)
            print(f"\n  {pair}:")
            for j, profile in enumerate(top_profiles, 1):
                print(f"    {j}. {profile['direction']} {profile['regime']} | "
                      f"WR: {profile['win_rate']:.1f}% | "
                      f"Trades: {profile['trade_count']:,} | "
                      f"Confidence: {profile['confidence']:.3f}")
        
        print(f"\n✅ Scout Profile Engine test complete!")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

# Integration notes for trade_scout.py:
"""
# In trade_scout.py __init__:
from scout_profiles import ScoutProfileEngine
self.profile_engine = ScoutProfileEngine()

# In scan loop after computing indicators:
match = self.profile_engine.match(
    pair=pair, 
    direction=direction, 
    regime=regime,
    rsi=current_rsi, 
    stoch_k=current_stoch_k,
    session=current_session, 
    candle_pattern=candle,
    bb_position=bb_pos
)

if match['confidence'] > 0.75:
    # High confidence — create alert with profile data
    alert['profile_confidence'] = match['confidence']
    alert['suggested_tp'] = match['suggested_tp_pips']
    alert['suggested_sl'] = match['suggested_sl_pips']
    alert['match_quality'] = match['match_quality']
    alert['historical_performance'] = f"{match['historical_win_rate']:.1f}% ({match['historical_trades']:,} trades)"
    
    # Record the finding for learning
    finding_id = self.profile_engine.record_finding(
        profile_key=match['profile_key'],
        entry_price=current_price,
        pair=pair,
        direction=direction,
        rsi=current_rsi,
        stoch_k=current_stoch_k,
        regime=regime,
        session=current_session,
        candle_pattern=candle,
        bb_zone=bb_pos,
        historical_confidence=match['historical_win_rate']/100,
        blended_confidence=match['confidence'],
        match_quality=match['match_quality']
    )
    alert['finding_id'] = finding_id

# Periodic maintenance (run daily):
self.profile_engine.resolve_pending_findings()  # Every 30 min
self.profile_engine.rebuild_daily()  # Once per day
"""
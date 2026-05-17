#!/usr/bin/env python3
"""
Market Sessions Module - Forex session timing and pair optimization

Provides optimal trading windows based on when currency markets are active.
Each forex pair has peak trading times when its underlying currencies' 
markets overlap, providing higher volatility and tighter spreads.

Session Times (ET):
- Sydney: 5PM-2AM ET
- Tokyo: 7PM-4AM ET  
- London: 3AM-12PM ET
- New York: 8AM-5PM ET

Usage:
    from market_sessions import get_active_sessions, is_prime_time
    
    active = get_active_sessions()
    if is_prime_time("EUR_USD"):
        print("EUR_USD is in prime trading window")
"""

import datetime
from typing import Dict, List, Tuple, Optional
import pytz

# Session definitions (all times in ET)
SESSIONS = {
    'Sydney': {
        'open': datetime.time(17, 0),   # 5PM ET
        'close': datetime.time(2, 0),   # 2AM ET (next day)
        'crosses_midnight': True
    },
    'Tokyo': {
        'open': datetime.time(19, 0),   # 7PM ET  
        'close': datetime.time(4, 0),   # 4AM ET (next day)
        'crosses_midnight': True
    },
    'London': {
        'open': datetime.time(3, 0),    # 3AM ET
        'close': datetime.time(12, 0),  # 12PM ET
        'crosses_midnight': False
    },
    'New_York': {
        'open': datetime.time(8, 0),    # 8AM ET
        'close': datetime.time(17, 0),  # 5PM ET
        'crosses_midnight': False
    }
}

# Pair to optimal session mapping
PAIR_SESSIONS = {
    # Major USD pairs - best during London-NY overlap
    'EUR_USD': ['London', 'New_York'],
    'GBP_USD': ['London', 'New_York'], 
    'USD_CHF': ['London', 'New_York'],
    
    # EUR cross - London dominates
    'EUR_GBP': ['London'],
    'EUR_CHF': ['London'],
    
    # JPY pairs - Tokyo-London overlap + London session
    'USD_JPY': ['Tokyo', 'London'],
    'EUR_JPY': ['Tokyo', 'London'],
    'GBP_JPY': ['Tokyo', 'London'],
    'AUD_JPY': ['Sydney', 'Tokyo', 'London'],
    
    # Oceania pairs - Sydney-Tokyo overlap + Asian crossover
    'AUD_USD': ['Sydney', 'Tokyo'],
    'NZD_USD': ['Sydney', 'Tokyo'],
    'AUD_NZD': ['Sydney'],
    
    # CAD pair - NY session
    'USD_CAD': ['New_York'],
    'CAD_JPY': ['Tokyo', 'New_York'],
    
    # Cross pairs involving EUR and AUD - London with Asian spillover
    'EUR_AUD': ['London', 'Sydney', 'Tokyo'],
}

def _get_et_time(now: Optional[datetime.datetime] = None) -> datetime.datetime:
    """Get current time in ET timezone."""
    if now is None:
        now = datetime.datetime.now(pytz.UTC)
    elif now.tzinfo is None:
        # Assume UTC if no timezone info
        now = pytz.UTC.localize(now)
    
    et_tz = pytz.timezone('US/Eastern')
    return now.astimezone(et_tz)

def _is_session_active(session_name: str, now: Optional[datetime.datetime] = None) -> bool:
    """Check if a trading session is currently active."""
    if session_name not in SESSIONS:
        return False
    
    et_time = _get_et_time(now)
    current_time = et_time.time()
    session = SESSIONS[session_name]
    
    open_time = session['open']
    close_time = session['close']
    
    if session['crosses_midnight']:
        # Session spans midnight (e.g., Sydney 5PM-2AM)
        return current_time >= open_time or current_time <= close_time
    else:
        # Session within same day (e.g., London 3AM-12PM)
        return open_time <= current_time <= close_time

def get_active_sessions(now: Optional[datetime.datetime] = None) -> List[str]:
    """Get list of currently active trading sessions.
    
    Args:
        now: Optional datetime to check. If None, uses current time.
        
    Returns:
        List of active session names (e.g., ['London', 'New_York'])
    """
    active = []
    for session_name in SESSIONS.keys():
        if _is_session_active(session_name, now):
            active.append(session_name)
    return active

def get_pair_best_windows(instrument: str) -> List[str]:
    """Get best trading windows for a currency pair.
    
    Args:
        instrument: Currency pair (e.g., 'EUR_USD')
        
    Returns:
        List of session names that are optimal for this pair
    """
    return PAIR_SESSIONS.get(instrument, [])

def is_prime_time(instrument: str, now: Optional[datetime.datetime] = None) -> bool:
    """Check if currency pair is in its optimal trading window.
    
    Args:
        instrument: Currency pair (e.g., 'EUR_USD')
        now: Optional datetime to check. If None, uses current time.
        
    Returns:
        True if at least one of the pair's optimal sessions is active
    """
    best_sessions = get_pair_best_windows(instrument)
    if not best_sessions:
        return False  # Unknown pair, not prime time
    
    active_sessions = get_active_sessions(now)
    
    # Return True if any of the pair's best sessions are active
    return bool(set(best_sessions) & set(active_sessions))

def get_session_quality(instrument: str, now: Optional[datetime.datetime] = None) -> float:
    """Get quality score (0-1) for current time for this pair.
    
    Scoring:
    - 1.0 = Peak overlap (multiple optimal sessions active)
    - 0.8 = Single optimal session active
    - 0.5 = Related session active (not optimal but tradeable)
    - 0.3 = Market hours but suboptimal sessions
    - 0.0 = Dead zone (no major sessions active)
    
    Args:
        instrument: Currency pair (e.g., 'EUR_USD')
        now: Optional datetime to check. If None, uses current time.
        
    Returns:
        Quality score from 0.0 (worst) to 1.0 (best)
    """
    best_sessions = get_pair_best_windows(instrument)
    active_sessions = get_active_sessions(now)
    
    if not active_sessions:
        return 0.0  # No sessions active - dead zone
    
    if not best_sessions:
        # Unknown pair - return moderate score if any session active
        return 0.3 if active_sessions else 0.0
    
    # Count optimal sessions that are active
    optimal_active = set(best_sessions) & set(active_sessions)
    optimal_count = len(optimal_active)
    
    if optimal_count >= 2:
        return 1.0  # Peak overlap - multiple optimal sessions
    elif optimal_count == 1:
        return 0.8  # Single optimal session active
    else:
        # No optimal sessions, but check for related sessions
        # If any session is active, it's tradeable but suboptimal
        return 0.5 if active_sessions else 0.0

def get_all_pairs_by_priority(now: Optional[datetime.datetime] = None) -> List[Tuple[str, float]]:
    """Get all pairs ordered by current session quality (best first).
    
    Args:
        now: Optional datetime to check. If None, uses current time.
        
    Returns:
        List of (instrument, quality_score) tuples, sorted by quality desc
    """
    pairs_with_quality = []
    
    for instrument in PAIR_SESSIONS.keys():
        quality = get_session_quality(instrument, now)
        pairs_with_quality.append((instrument, quality))
    
    # Sort by quality descending (best first)
    pairs_with_quality.sort(key=lambda x: x[1], reverse=True)
    
    return pairs_with_quality

def get_session_overlaps(now: Optional[datetime.datetime] = None) -> List[Tuple[str, str]]:
    """Get currently active session overlaps (premium trading windows).
    
    Args:
        now: Optional datetime to check. If None, uses current time.
        
    Returns:
        List of (session1, session2) tuples for overlapping sessions
    """
    active = get_active_sessions(now)
    overlaps = []
    
    # Generate all unique pairs of active sessions
    for i, session1 in enumerate(active):
        for session2 in active[i+1:]:
            overlaps.append((session1, session2))
    
    return overlaps

def get_next_prime_window(instrument: str, now: Optional[datetime.datetime] = None) -> Optional[Dict[str, str]]:
    """Get the next prime trading window for a pair.
    
    Args:
        instrument: Currency pair (e.g., 'EUR_USD')
        now: Optional datetime to check. If None, uses current time.
        
    Returns:
        Dict with 'session', 'opens_at', 'closes_at' or None if no upcoming window
    """
    best_sessions = get_pair_best_windows(instrument)
    if not best_sessions:
        return None
    
    et_now = _get_et_time(now)
    current_time = et_now.time()
    
    # Find next opening session
    upcoming = []
    
    for session_name in best_sessions:
        session = SESSIONS[session_name]
        open_time = session['open']
        close_time = session['close']
        
        # Calculate time until session opens
        if session['crosses_midnight']:
            # Session like Sydney (5PM-2AM)
            if current_time <= close_time:
                # We're in the early morning part, session is active
                continue
            elif current_time >= open_time:
                # Session is currently active
                continue
            else:
                # Session opens later today
                today = et_now.date()
                opens_at = datetime.datetime.combine(today, open_time)
                opens_at = pytz.timezone('US/Eastern').localize(opens_at)
        else:
            # Session like London (3AM-12PM)
            if open_time <= current_time <= close_time:
                # Session is currently active
                continue
            elif current_time < open_time:
                # Session opens later today
                today = et_now.date()
                opens_at = datetime.datetime.combine(today, open_time)
                opens_at = pytz.timezone('US/Eastern').localize(opens_at)
            else:
                # Session opens tomorrow
                tomorrow = et_now.date() + datetime.timedelta(days=1)
                opens_at = datetime.datetime.combine(tomorrow, open_time)
                opens_at = pytz.timezone('US/Eastern').localize(opens_at)
        
        # Calculate close time
        if session['crosses_midnight'] and open_time > close_time:
            # Close time is next day
            if opens_at.time() >= open_time:
                # Opening today, closing tomorrow
                close_date = opens_at.date() + datetime.timedelta(days=1)
            else:
                # Opening tomorrow, closing day after
                close_date = opens_at.date() + datetime.timedelta(days=1)
        else:
            close_date = opens_at.date()
        
        closes_at = datetime.datetime.combine(close_date, close_time)
        closes_at = pytz.timezone('US/Eastern').localize(closes_at)
        
        upcoming.append({
            'session': session_name,
            'opens_at': opens_at.isoformat(),
            'closes_at': closes_at.isoformat(),
            'minutes_until': int((opens_at - et_now).total_seconds() / 60)
        })
    
    if not upcoming:
        return None
    
    # Return the earliest upcoming window
    return min(upcoming, key=lambda x: x['minutes_until'])

def get_dead_zones(instrument: str, now: Optional[datetime.datetime] = None) -> List[Dict[str, str]]:
    """Get upcoming dead zones (periods with no optimal sessions) for a pair.
    
    Args:
        instrument: Currency pair (e.g., 'EUR_USD')  
        now: Optional datetime to check. If None, uses current time.
        
    Returns:
        List of dicts with 'starts_at', 'ends_at', 'duration_hours'
    """
    # This is a simplified implementation - could be expanded
    # For now, identify the gap between NY close and London open
    et_now = _get_et_time(now)
    
    # For most pairs, the dead zone is roughly 5PM ET to 3AM ET (NY close to London open)
    dead_zones = []
    
    today = et_now.date()
    
    # NY closes at 5PM
    ny_close = datetime.datetime.combine(today, datetime.time(17, 0))
    ny_close = pytz.timezone('US/Eastern').localize(ny_close)
    
    # London opens at 3AM next day
    tomorrow = today + datetime.timedelta(days=1)
    london_open = datetime.datetime.combine(tomorrow, datetime.time(3, 0))
    london_open = pytz.timezone('US/Eastern').localize(london_open)
    
    if et_now < ny_close:
        # Dead zone is from NY close today to London open tomorrow
        dead_zones.append({
            'starts_at': ny_close.isoformat(),
            'ends_at': london_open.isoformat(),
            'duration_hours': 10.0
        })
    elif et_now > london_open:
        # Next dead zone is from NY close tomorrow to London open day after
        ny_close_tomorrow = ny_close + datetime.timedelta(days=1)
        london_open_day_after = london_open + datetime.timedelta(days=1)
        
        dead_zones.append({
            'starts_at': ny_close_tomorrow.isoformat(),
            'ends_at': london_open_day_after.isoformat(),
            'duration_hours': 10.0
        })
    
    return dead_zones


# Test/demo functions
if __name__ == "__main__":
    import json
    
    print("=== Market Sessions Demo ===\n")
    
    # Current status
    now = datetime.datetime.now()
    active = get_active_sessions()
    print(f"Currently active sessions: {active}")
    
    # Test major pairs
    test_pairs = ['EUR_USD', 'GBP_USD', 'USD_JPY', 'AUD_USD', 'USD_CAD']
    
    print(f"\nPair priorities at {now.strftime('%H:%M ET')}:")
    for pair, quality in get_all_pairs_by_priority()[:5]:
        prime = "✓" if is_prime_time(pair) else "✗"
        print(f"  {pair:<8} {quality:.1f} {prime}")
    
    print(f"\nSession overlaps: {get_session_overlaps()}")
    
    # Next windows
    print(f"\nNext prime windows:")
    for pair in test_pairs:
        next_window = get_next_prime_window(pair)
        if next_window:
            print(f"  {pair}: {next_window['session']} in {next_window['minutes_until']}min")
        else:
            print(f"  {pair}: Currently in prime time or no upcoming window")
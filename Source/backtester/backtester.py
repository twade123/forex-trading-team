#!/usr/bin/env python3
"""Core backtesting engine: walks through candles, simulates trades."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import indicators, divergence, rule_engine

logger = logging.getLogger(__name__)


class Position:
    """Track an open position."""

    def __init__(self, direction: str, entry_price: float, entry_time: str,
                 stop_loss: float, take_profit_1: float, risk_pips: float,
                 rules_fired: list, regime: str, confluence_score: int):
        self.direction = direction  # "buy" or "sell"
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.stop_loss = stop_loss
        self.take_profit_1 = take_profit_1
        self.risk_pips = risk_pips
        self.rules_fired = rules_fired
        self.regime = regime
        self.confluence_score = confluence_score
        self.half_exited = False
        self.exit_price = None
        self.exit_time = None
        self.exit_reason = None
        self.pips = 0.0


class Backtester:
    """Walk through historical candles and simulate trading."""

    def __init__(self, confluence_threshold: int = 60, risk_reward: float = 2.0,
                 max_positions: int = 1):
        self.threshold = confluence_threshold
        self.risk_reward = risk_reward
        self.max_positions = max_positions
        self.sl_atr_mult = 1.5  # Can be overridden
        self.is_jpy = False     # JPY pairs use /100 instead of *10000
        self.rules = rule_engine.load_rules()
        self.trades: List[dict] = []
        self.positions: List[Position] = []

    def prepare_data(self, csv_path: str) -> pd.DataFrame:
        """Load CSV, compute all indicators and divergence signals."""
        logger.info("Loading data from %s", csv_path)
        df = pd.read_csv(csv_path)
        df["timestamp"] = pd.to_datetime(df["timestamp"])

        logger.info("Computing indicators on %d candles...", len(df))
        df = indicators.compute_all(df)

        logger.info("Detecting divergence signals...")
        df = divergence.add_divergence_signals(df)

        # Add derived columns for rule engine
        df["prev_macd_histogram"] = df["macd_histogram"].shift(1)
        df["prev_adx"] = df["adx"].shift(1)
        df["prev_sma_50"] = df["sma_50"].shift(1)
        df["prev_sma_100"] = df["sma_100"].shift(1)
        df["prev_stoch_k"] = df["stoch_k"].shift(1)
        df["prev_stoch_d"] = df["stoch_d"].shift(1)
        df["avg_volume"] = df["volume"].rolling(20).mean()
        df["atr_avg"] = df["atr"].rolling(50).mean()

        # MACD crossover recency
        macd_cross = ((df["macd_histogram"] > 0) & (df["prev_macd_histogram"] <= 0)) | \
                     ((df["macd_histogram"] < 0) & (df["prev_macd_histogram"] >= 0))
        bars_since = pd.Series(np.nan, index=df.index)
        last_cross = -999
        for i in range(len(df)):
            if macd_cross.iloc[i]:
                last_cross = i
            bars_since.iloc[i] = i - last_cross
        df["macd_cross_bars_ago"] = bars_since

        # Hour of day (for session filters)
        df["hour"] = df["timestamp"].dt.hour
        df["day_of_week"] = df["timestamp"].dt.dayofweek
        df["year"] = df["timestamp"].dt.year
        df["month"] = df["timestamp"].dt.month

        logger.info("Data prepared: %d candles with %d columns", len(df), len(df.columns))
        return df

    def _row_to_dict(self, row) -> dict:
        """Convert a DataFrame row to a dict for the rule engine."""
        d = {}
        for col in row.index:
            val = row[col]
            if isinstance(val, (np.bool_, bool)):
                d[col] = bool(val)
            elif isinstance(val, (np.integer,)):
                d[col] = int(val)
            elif isinstance(val, (np.floating,)):
                d[col] = float(val) if not np.isnan(val) else 0.0
            else:
                d[col] = val
        return d

    def _pip_mult(self) -> float:
        """Return pip multiplier for the pair type."""
        return 100.0 if self.is_jpy else 10000.0

    def _calculate_stop_loss(self, row: dict, direction: str) -> float:
        """Calculate stop-loss based on ATR."""
        atr_val = row.get("atr", 0.001)
        multiplier = self.sl_atr_mult
        if direction == "buy":
            return row["close"] - (atr_val * multiplier)
        else:
            return row["close"] + (atr_val * multiplier)

    def _check_position_exits(self, pos: Position, row: dict) -> Optional[dict]:
        """Check if a position should be exited."""
        close = row["close"]
        high = row["high"]
        low = row["low"]

        # Check stop-loss
        if pos.direction == "buy" and low <= pos.stop_loss:
            exit_price = pos.stop_loss
            pips = (exit_price - pos.entry_price) * self._pip_mult()
            if pos.half_exited:
                pips *= 0.5
            return {"exit_price": exit_price, "reason": "stop_loss", "pips": pips}

        if pos.direction == "sell" and high >= pos.stop_loss:
            exit_price = pos.stop_loss
            pips = (pos.entry_price - exit_price) * self._pip_mult()
            if pos.half_exited:
                pips *= 0.5
            return {"exit_price": exit_price, "reason": "stop_loss", "pips": pips}

        # Check take-profit 1 (half exit)
        if not pos.half_exited:
            if pos.direction == "buy" and high >= pos.take_profit_1:
                half_pips = (pos.take_profit_1 - pos.entry_price) * self._pip_mult() * 0.5
                pos.half_exited = True
                pos.stop_loss = pos.entry_price  # Move to breakeven
                pos.pips += half_pips
                return None  # Don't fully exit yet

            if pos.direction == "sell" and low <= pos.take_profit_1:
                half_pips = (pos.entry_price - pos.take_profit_1) * self._pip_mult() * 0.5
                pos.half_exited = True
                pos.stop_loss = pos.entry_price
                pos.pips += half_pips
                return None

        # Check SMA break exit (for remaining half)
        if pos.half_exited:
            sma50 = row.get("sma_50", 0)
            pip_threshold = 0.100 if self.is_jpy else 0.0010  # 10 pips
            if pos.direction == "buy" and close < sma50 - pip_threshold:
                exit_price = close
                pips = (exit_price - pos.entry_price) * self._pip_mult() * 0.5
                return {"exit_price": exit_price, "reason": "sma_break", "pips": pos.pips + pips}

            if pos.direction == "sell" and close > sma50 + pip_threshold:
                exit_price = close
                pips = (pos.entry_price - exit_price) * self._pip_mult() * 0.5
                return {"exit_price": exit_price, "reason": "sma_break", "pips": pos.pips + pips}

        return None

    def run(self, df: pd.DataFrame) -> dict:
        """Run the backtest on prepared data."""
        logger.info("Running backtest with threshold=%d on %d candles...", self.threshold, len(df))

        # Skip first 200 candles (indicator warmup)
        start_idx = 200

        for i in range(start_idx, len(df)):
            row = self._row_to_dict(df.iloc[i])

            # Check existing positions for exits
            positions_to_close = []
            for j, pos in enumerate(self.positions):
                exit_info = self._check_position_exits(pos, row)
                if exit_info:
                    pos.exit_price = exit_info["exit_price"]
                    pos.exit_time = str(row.get("timestamp", ""))
                    pos.exit_reason = exit_info["reason"]
                    pos.pips = exit_info["pips"]
                    self.trades.append(self._position_to_trade(pos))
                    positions_to_close.append(j)

            for j in sorted(positions_to_close, reverse=True):
                self.positions.pop(j)

            # Skip if we have max positions
            if len(self.positions) >= self.max_positions:
                continue

            # Detect regime
            regime = rule_engine.detect_regime(row)

            # Check skip rules
            skips = rule_engine.evaluate_skip_rules(row, self.rules)
            if skips:
                continue

            # Check entry rules
            fired = rule_engine.evaluate_entry_rules(row, self.rules, regime)
            if not fired:
                continue

            # Score confluence
            confluence = rule_engine.score_confluence(fired)
            if confluence["score"] < self.threshold:
                continue

            direction = confluence["direction"]
            if direction == "hold":
                continue

            # Calculate stop-loss and take-profit
            stop_loss = self._calculate_stop_loss(row, direction)
            risk = abs(row["close"] - stop_loss)
            if direction == "buy":
                take_profit_1 = row["close"] + risk * self.risk_reward
            else:
                take_profit_1 = row["close"] - risk * self.risk_reward

            risk_pips = risk * self._pip_mult()

            # Open position
            pos = Position(
                direction=direction,
                entry_price=row["close"],
                entry_time=str(row.get("timestamp", "")),
                stop_loss=stop_loss,
                take_profit_1=take_profit_1,
                risk_pips=risk_pips,
                rules_fired=confluence["rules_fired"],
                regime=regime,
                confluence_score=confluence["score"],
            )
            self.positions.append(pos)

        # Close any remaining positions at last price
        if self.positions:
            last_row = self._row_to_dict(df.iloc[-1])
            for pos in self.positions:
                if pos.direction == "buy":
                    pips = (last_row["close"] - pos.entry_price) * self._pip_mult()
                else:
                    pips = (pos.entry_price - last_row["close"]) * self._pip_mult()
                if pos.half_exited:
                    pips = pos.pips + pips * 0.5
                pos.exit_price = last_row["close"]
                pos.exit_time = str(last_row.get("timestamp", ""))
                pos.exit_reason = "end_of_data"
                pos.pips = pips
                self.trades.append(self._position_to_trade(pos))
            self.positions = []

        return self._compute_stats()

    def _position_to_trade(self, pos: Position) -> dict:
        return {
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "entry_time": pos.entry_time,
            "exit_price": pos.exit_price,
            "exit_time": pos.exit_time,
            "exit_reason": pos.exit_reason,
            "pips": round(pos.pips, 1),
            "risk_pips": round(pos.risk_pips, 1),
            "rules_fired": pos.rules_fired,
            "regime": pos.regime,
            "confluence_score": pos.confluence_score,
            "half_exited": pos.half_exited,
        }

    def _compute_stats(self) -> dict:
        """Compute comprehensive backtest statistics."""
        if not self.trades:
            return {"total_trades": 0, "error": "No trades generated"}

        wins = [t for t in self.trades if t["pips"] > 0]
        losses = [t for t in self.trades if t["pips"] <= 0]

        total_pips = sum(t["pips"] for t in self.trades)
        gross_profit = sum(t["pips"] for t in wins) if wins else 0
        gross_loss = abs(sum(t["pips"] for t in losses)) if losses else 1

        # Max drawdown
        running_pips = 0
        peak = 0
        max_dd = 0
        for t in self.trades:
            running_pips += t["pips"]
            peak = max(peak, running_pips)
            dd = peak - running_pips
            max_dd = max(max_dd, dd)

        # By regime
        regime_stats = {}
        for regime in ["trending", "ranging", "transitional"]:
            regime_trades = [t for t in self.trades if t["regime"] == regime]
            if regime_trades:
                regime_wins = [t for t in regime_trades if t["pips"] > 0]
                regime_stats[regime] = {
                    "trades": len(regime_trades),
                    "win_rate": round(len(regime_wins) / len(regime_trades) * 100, 1),
                    "total_pips": round(sum(t["pips"] for t in regime_trades), 1),
                }

        # By rule
        rule_stats = {}
        for t in self.trades:
            for rule_id in t["rules_fired"]:
                if rule_id not in rule_stats:
                    rule_stats[rule_id] = {"trades": 0, "wins": 0, "total_pips": 0}
                rule_stats[rule_id]["trades"] += 1
                rule_stats[rule_id]["total_pips"] += t["pips"]
                if t["pips"] > 0:
                    rule_stats[rule_id]["wins"] += 1

        for rule_id in rule_stats:
            s = rule_stats[rule_id]
            s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0
            s["total_pips"] = round(s["total_pips"], 1)

        # By hour
        hour_stats = {}
        for t in self.trades:
            try:
                hour = int(t["entry_time"].split("T")[1].split(":")[0]) if "T" in t["entry_time"] else 0
            except (IndexError, ValueError):
                hour = 0
            if hour not in hour_stats:
                hour_stats[hour] = {"trades": 0, "wins": 0, "pips": 0}
            hour_stats[hour]["trades"] += 1
            hour_stats[hour]["pips"] += t["pips"]
            if t["pips"] > 0:
                hour_stats[hour]["wins"] += 1

        for h in hour_stats:
            s = hour_stats[h]
            s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0
            s["pips"] = round(s["pips"], 1)

        # By year
        year_stats = {}
        for t in self.trades:
            try:
                year = t["entry_time"][:4]
            except (IndexError, TypeError):
                year = "unknown"
            if year not in year_stats:
                year_stats[year] = {"trades": 0, "wins": 0, "pips": 0}
            year_stats[year]["trades"] += 1
            year_stats[year]["pips"] += t["pips"]
            if t["pips"] > 0:
                year_stats[year]["wins"] += 1

        for y in year_stats:
            s = year_stats[y]
            s["win_rate"] = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0
            s["pips"] = round(s["pips"], 1)

        return {
            "confluence_threshold": self.threshold,
            "total_trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(self.trades) * 100, 1),
            "total_pips": round(total_pips, 1),
            "avg_win_pips": round(np.mean([t["pips"] for t in wins]), 1) if wins else 0,
            "avg_loss_pips": round(np.mean([t["pips"] for t in losses]), 1) if losses else 0,
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
            "max_drawdown_pips": round(max_dd, 1),
            "by_regime": regime_stats,
            "by_rule": dict(sorted(rule_stats.items(), key=lambda x: x[1]["win_rate"], reverse=True)),
            "by_hour": dict(sorted(hour_stats.items())),
            "by_year": dict(sorted(year_stats.items())),
            "trade_log": self.trades,
        }

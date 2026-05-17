#!/usr/bin/env python3
"""
Lightweight JourneyTracker for the trading bot.

Provides the same track_journey_step() interface as BoardRoom but only does 
SQLite inserts to v2/journeys.db. No spaCy, no Trevor Core, no database discovery,
no workspace cache.

Designed for fast startup and minimal memory footprint.
"""

import os
import sqlite3
import time
import json
import logging
from threading import Lock

logger = logging.getLogger(__name__)


class JourneyTracker:
    """Lightweight journey tracking that only does SQLite inserts."""
    
    def __init__(self, db_path: str):
        """Initialize with path to v2/journeys.db."""
        self.db_path = db_path
        self._conn = None
        self._lock = Lock()
        
        # Ensure database exists and has the required table
        self._init_database()
    
    def _init_database(self):
        """Initialize database schema using pooled connection."""
        try:
            conn = self._get_conn()
            conn.execute('''
                CREATE TABLE IF NOT EXISTS journey_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    journey_id TEXT,
                    step_type TEXT,
                    step_name TEXT,
                    description TEXT,
                    input_data TEXT,
                    output_data TEXT,
                    error TEXT,
                    timestamp REAL,
                    duration REAL,
                    status TEXT,
                    metadata TEXT,
                    FOREIGN KEY (journey_id) REFERENCES request_journeys(journey_id)
                )
            ''')
            conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_journey_steps_journey_id
                ON journey_steps(journey_id)
            ''')
            conn.commit()
            logger.info(f"JourneyTracker initialized with database: {self.db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize JourneyTracker database: {e}")
            raise

    def _get_conn(self):
        """Get a pooled connection to journeys.db."""
        try:
            from db_pool import get_journeys
            return get_journeys()
        except ImportError:
            # Fallback for non-trading-team usage
            from db_connection import get_db
            return get_db(self.db_path).__enter__()
    
    def track_journey_step(self, journey_id, step_type=None, step_name=None, 
                          description=None, input_data=None, output_data=None, 
                          error=None, metadata=None, **kwargs):
        """
        Track a journey step with the same signature as BoardRoom.track_journey_step().
        
        Args:
            journey_id: Journey identifier
            step_type: Type of step (optional)  
            step_name: Name of step (optional)
            description: Description of step (optional)
            input_data: Input data for step (optional)
            output_data: Output data for step (optional)  
            error: Error message if step failed (optional)
            metadata: Additional metadata (optional)
            **kwargs: Additional arguments (ignored for compatibility)
        """
        
        with self._lock:
            try:
                # Convert complex objects to JSON strings
                input_data_str = None
                output_data_str = None
                metadata_str = None
                
                if input_data is not None:
                    if isinstance(input_data, (dict, list)):
                        input_data_str = json.dumps(input_data)
                    else:
                        input_data_str = str(input_data)
                
                if output_data is not None:
                    if isinstance(output_data, (dict, list)):
                        output_data_str = json.dumps(output_data)
                    else:
                        output_data_str = str(output_data)
                
                if metadata is not None:
                    if isinstance(metadata, (dict, list)):
                        metadata_str = json.dumps(metadata)
                    else:
                        metadata_str = str(metadata)
                
                # Insert the journey step using pooled connection
                conn = self._get_conn()
                conn.execute('''
                    INSERT INTO journey_steps (
                        journey_id, step_type, step_name, description,
                        input_data, output_data, error, metadata, timestamp, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    journey_id,
                    step_type,
                    step_name,
                    description,
                    input_data_str,
                    output_data_str,
                    error,
                    metadata_str,
                    time.time(),
                    'completed' if error is None else 'error'
                ))
                conn.commit()
                
            except Exception as e:
                logger.error(f"Failed to track journey step: {e}")
                # Don't re-raise to avoid breaking the trading bot on tracking failures
    
    def close(self):
        """No-op: connections are now per-call."""
        pass
    
    def __del__(self):
        """No-op: connections are now per-call."""
        pass
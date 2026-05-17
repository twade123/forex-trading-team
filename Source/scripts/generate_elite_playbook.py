#!/usr/bin/env python3
"""
Elite Playbook Generator

Extracts elite trading setups from the database and creates configuration files
for the Trade Scout system.

Criteria for elite setups:
- win_rate >= 88%
- trade_count >= 1000  
- profit_factor > 1.2
"""

import json
import sqlite3
import pandas as pd
from pathlib import Path

def extract_elite_playbook(db_path: str = "~/jarvis/Database/v2/trading_forex.db",
                          output_path: str = "elite_playbook.json"):
    """Extract elite setups and save as JSON configuration."""
    
    try:
        with sqlite3.connect(db_path) as conn:
            query = """
                SELECT setup, pair, timeframe, regime, win_rate, trade_count, profit_factor,
                       avg_pips, avg_risk_reward, max_favorable, max_adverse, avg_hold_time,
                       h4_agrees_count, h4_agrees_win_rate, best_session, best_session_win_rate
                FROM backtest_setup_performance 
                WHERE win_rate >= 88 
                AND trade_count >= 1000 
                AND profit_factor > 1.2
                ORDER BY win_rate DESC, profit_factor DESC, trade_count DESC
            """
            
            df = pd.read_sql_query(query, conn)
            
            if df.empty:
                print("No elite setups found matching criteria!")
                return
            
            # Convert to structured format
            elite_setups = df.to_dict('records')
            
            # Add metadata
            config = {
                'metadata': {
                    'generated_at': pd.Timestamp.now().isoformat(),
                    'criteria': {
                        'min_win_rate': 88.0,
                        'min_trade_count': 1000,
                        'min_profit_factor': 1.2
                    },
                    'total_setups': len(elite_setups)
                },
                'setups': elite_setups
            }
            
            # Save to JSON
            output_file = Path(__file__).parent / output_path
            with open(output_file, 'w') as f:
                json.dump(config, f, indent=2, default=str)
            
            print(f"Elite playbook generated: {output_file}")
            print(f"Found {len(elite_setups)} elite setups")
            
            # Print summary by pair
            summary = df.groupby('pair').agg({
                'setup': 'count',
                'win_rate': 'mean',
                'profit_factor': 'mean',
                'trade_count': 'sum'
            }).round(2)
            
            print("\nSummary by pair:")
            print(summary)
            
            # Print top setups
            print(f"\nTop 10 setups by win rate:")
            top_setups = df.head(10)[['setup', 'pair', 'win_rate', 'trade_count', 'profit_factor']]
            print(top_setups.to_string(index=False))
            
            return config
            
    except Exception as e:
        print(f"Error extracting playbook: {e}")
        return None


def analyze_playbook_coverage(config):
    """Analyze which pairs and timeframes are covered."""
    if not config:
        return
        
    setups = pd.DataFrame(config['setups'])
    
    print("\n" + "="*50)
    print("PLAYBOOK COVERAGE ANALYSIS")
    print("="*50)
    
    # Coverage by pair
    pair_coverage = setups.groupby('pair').agg({
        'setup': 'count',
        'win_rate': ['mean', 'min', 'max'],
        'profit_factor': ['mean', 'min', 'max']
    }).round(2)
    
    print("Coverage by pair:")
    print(pair_coverage)
    
    # Coverage by timeframe
    tf_coverage = setups.groupby('timeframe').agg({
        'setup': 'count',
        'win_rate': 'mean',
        'profit_factor': 'mean'
    }).round(2)
    
    print("\nCoverage by timeframe:")
    print(tf_coverage)
    
    # Top performing setups by name
    setup_performance = setups.groupby('setup').agg({
        'pair': 'count',
        'win_rate': 'mean',
        'profit_factor': 'mean',
        'trade_count': 'sum'
    }).round(2)
    setup_performance.rename(columns={'pair': 'pair_count'}, inplace=True)
    setup_performance = setup_performance.sort_values('win_rate', ascending=False)
    
    print("\nTop setup types:")
    print(setup_performance.head(10))


def create_scout_thresholds(config):
    """Create score thresholds for each setup based on historical performance."""
    if not config:
        return
    
    setups = pd.DataFrame(config['setups'])
    
    # Calculate dynamic thresholds based on performance
    # Higher win rate = lower threshold (easier to trigger)
    # Higher profit factor = lower threshold
    # More trades = more confident threshold
    
    thresholds = {}
    
    for _, row in setups.iterrows():
        setup_key = f"{row['setup']}_{row['pair']}_{row['timeframe']}"
        
        # Base threshold
        base_threshold = 15
        
        # Adjust based on win rate (higher win rate = lower threshold)
        wr_adjustment = (95 - row['win_rate']) * 0.2
        
        # Adjust based on profit factor (higher PF = lower threshold) 
        pf_adjustment = max(0, (2.0 - row['profit_factor']) * 2)
        
        # Adjust based on trade count (more trades = more confidence = lower threshold)
        tc_adjustment = max(0, (2000 - row['trade_count']) / 1000 * 2)
        
        threshold = base_threshold + wr_adjustment + pf_adjustment + tc_adjustment
        threshold = max(8, min(25, threshold))  # Clamp between 8-25
        
        thresholds[setup_key] = {
            'threshold': round(threshold, 1),
            'win_rate': row['win_rate'],
            'profit_factor': row['profit_factor'],
            'trade_count': row['trade_count']
        }
    
    # Save thresholds
    output_file = Path(__file__).parent / 'scout_thresholds.json'
    with open(output_file, 'w') as f:
        json.dump(thresholds, f, indent=2)
    
    print(f"\nScout thresholds saved to: {output_file}")
    
    return thresholds


if __name__ == "__main__":
    print("Elite Playbook Generator")
    print("=======================")
    
    # Check if database exists
    db_path = "~/jarvis/Database/v2/trading_forex.db"
    if not Path(db_path).exists():
        print(f"Database not found: {db_path}")
        print("Please check the database path.")
        exit(1)
    
    # Extract elite playbook
    config = extract_elite_playbook()
    
    if config:
        # Analyze coverage
        analyze_playbook_coverage(config)
        
        # Create scout thresholds
        create_scout_thresholds(config)
        
        print(f"\nElite playbook system ready!")
        print(f"- {len(config['setups'])} elite setups identified")
        print(f"- Minimum win rate: {min(s['win_rate'] for s in config['setups']):.1f}%")
        print(f"- Average win rate: {sum(s['win_rate'] for s in config['setups']) / len(config['setups']):.1f}%")
        print(f"- Total historical trades: {sum(s['trade_count'] for s in config['setups']):,}")
    else:
        print("Failed to generate elite playbook!")
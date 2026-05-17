#!/usr/bin/env python3
"""
Trading System Launcher

Starts both the Trade Scout and Dashboard Server in parallel.
Use this to run the complete trading system.
"""

import importlib
import subprocess
import sys
import time
import signal
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class TradingSystemLauncher:
    def __init__(self):
        self.processes = []
        self.running = True
        
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
    
    def signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        print(f"\nReceived signal {signum}, shutting down...")
        self.running = False
        self.shutdown()
    
    def start_dashboard(self):
        """Start the dashboard server."""
        print("Starting Dashboard Server on http://localhost:8766")
        
        # serve_ui.py lives in the jarvis root (not Source/), launch from there
        cmd = [sys.executable, str(Path(__file__).parent.parent.parent / "serve_ui.py")]
        process = subprocess.Popen(
            cmd,
            cwd=Path(__file__).parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        self.processes.append(('dashboard', process))
        return process
    
    def start_trade_scout(self):
        """Start the trade scout."""
        print("Starting Trade Scout...")
        
        cmd = [sys.executable, "-m", "trade_scout"]
        process = subprocess.Popen(
            cmd,
            cwd=Path(__file__).parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1
        )
        
        self.processes.append(('trade_scout', process))
        return process
    
    def monitor_processes(self):
        """Monitor both processes and restart if needed."""
        print("Monitoring processes... Press Ctrl+C to stop")
        
        while self.running:
            for name, process in self.processes[:]:  # Copy list to avoid modification issues
                if process.poll() is not None:  # Process has terminated
                    print(f"{name} process terminated unexpectedly (exit code: {process.returncode})")
                    
                    # Read any remaining output
                    try:
                        output = process.stdout.read()
                        if output:
                            print(f"{name} final output: {output}")
                    except Exception as e:
                        logger.warning("[LAUNCHER] Failed to read process output for %s: %s", name, e)
                    
                    if self.running:  # Only restart if we're not shutting down
                        print(f"Restarting {name}...")
                        if name == 'dashboard':
                            new_process = self.start_dashboard()
                        else:
                            new_process = self.start_trade_scout()
                        
                        # Update the process in our list
                        self.processes = [(n, p) if n != name else (name, new_process) 
                                        for n, p in self.processes]
            
            time.sleep(5)  # Check every 5 seconds
    
    def shutdown(self):
        """Gracefully shutdown all processes."""
        print("Shutting down all processes...")

        # Stop Connection Sentry first (writes final report)
        try:
            from connection_sentry import sentry
            sentry.stop()
        except Exception:
            pass
        
        for name, process in self.processes:
            if process.poll() is None:  # Process is still running
                print(f"Terminating {name}...")
                try:
                    process.terminate()
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    print(f"Force killing {name}...")
                    process.kill()
                    process.wait()
        
        print("All processes terminated.")
    
    def run(self):
        """Run the complete trading system."""
        try:
            # Start both services
            dashboard_process = self.start_dashboard()
            time.sleep(2)  # Give dashboard time to start
            
            scout_process = self.start_trade_scout()
            time.sleep(2)
            
            # Start Connection Sentry (health monitoring for all connections)
            try:
                from connection_sentry import sentry
                sentry.start()
                print("Connection Sentry: STARTED (monitoring all connections)")
            except Exception as _sentry_err:
                print(f"Connection Sentry: FAILED to start ({_sentry_err})")
                logger.warning("[LAUNCHER] Sentry start failed: %s", _sentry_err)

            print("\n" + "="*60)
            print("FOREX TRADING SYSTEM STARTED")
            print("="*60)
            print("Dashboard: http://localhost:8766")
            print("Scout WebSocket: ws://localhost:8767")
            print("Sentry Report: dashboard/sentry_report.json")
            print("Sentry API: http://localhost:8766/api/trading/sentry")
            print("="*60)
            print()

            # Monitor processes
            self.monitor_processes()
            
        except KeyboardInterrupt:
            print("\nKeyboard interrupt received")
        except Exception as e:
            logger.error(f"Error in main loop: {e}")
        finally:
            self.shutdown()


def install_dependencies():
    """Check and install required dependencies."""
    required_packages = [
        'flask',
        'flask-socketio',
        'websockets',
        'pandas',
        'numpy',
        'requests'
    ]
    
    missing_packages = []
    
    for package in required_packages:
        try:
            importlib.import_module(package.replace('-', '_'))
        except ImportError:
            missing_packages.append(package)
    
    if missing_packages:
        print("Missing required packages:", missing_packages)
        print("Install with: pip install", " ".join(missing_packages))
        return False
    
    return True


if __name__ == "__main__":
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("Forex Trading System Launcher")
    print("============================")

    # ── FUSE cleanup: MUST run before ANY database connections ──────────
    # Purge stale .fuse_hidden files and orphaned -shm files left by
    # crashed processes. Without this, mmap collisions cause disk I/O errors.
    try:
        from fuse_cleanup import cleanup_fuse_artifacts
        result = cleanup_fuse_artifacts()
        if result.get("fuse_hidden_removed") or result.get("shm_removed"):
            print(f"FUSE cleanup: removed {result['fuse_hidden_removed']} ghost files, "
                  f"{result['shm_removed']} stale -shm files")
    except Exception as e:
        print(f"FUSE cleanup warning: {e}")
    # ───────────────────────────────────────────────────────────────────

    # Check dependencies
    if not install_dependencies():
        print("Please install missing dependencies and try again.")
        sys.exit(1)

    # Create and run launcher
    launcher = TradingSystemLauncher()
    launcher.run()
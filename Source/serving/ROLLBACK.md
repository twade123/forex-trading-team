# Serving Gateway — Rollback Playbook

If the gateway is misbehaving (timeouts, dropped requests, latency
explosions, queue backed up), this is how you get trading back to
direct `:11502` calls fast. Read all the way through before reverting —
in most cases stopping the gateway *process* is enough and you don't
need to revert any code.

## TL;DR — emergency, trading is bleeding pips

```bash
# 1. Stop the gateway. Callers now fail (nothing on :11503).
cat ~/.cache/forex_trading/pids/serving_gateway.pid | xargs kill 2>/dev/null
rm -f ~/.cache/forex_trading/pids/serving_gateway.pid

# 2. Revert all gateway-routing commits in one go. Trading callers
#    go back to :11502 direct, ghost callers go back to :11502 direct,
#    swarm CSO goes back to :11502 direct.
cd ~/Jarvis
git revert --no-edit 05db7f1e 64ca8618 4dd89d1e

# 3. Restart trading. Gateway commit (14a89c9f) and the serving/
#    package can stay — they're inert if nothing routes to them.
cd "Forex Trading Team/Source"
./trading_launcher.sh restart
```

That puts the world back to before the migration. Done.

## Before reverting — try these first

The gateway is a thin proxy. Most failures are operational, not
structural. Burn 60 seconds on these before touching git.

### 1. Is the gateway process alive?

```bash
lsof -nP -iTCP:11503 -sTCP:LISTEN
curl -s http://127.0.0.1:11503/healthz
curl -s http://127.0.0.1:11503/readyz
```

- `/healthz` — gateway itself is up
- `/readyz` — gateway *plus* at least one MLX backend is up

If `/readyz` 503s, MLX is the problem, not the gateway. Check
`:11502` directly: `curl -s http://127.0.0.1:11502/v1/models`.

### 2. Is the queue backed up?

```bash
curl -s http://127.0.0.1:11503/metrics | grep -E "queue_depth|requests_total|backend_errors"
```

If `queue_depth` is climbing and `backend_errors` isn't, MLX is just
slow — not a gateway bug. Bumping `worker_pool.size` in
`config.yaml` won't help (one backend = one in-flight slot).
Bumping `in_flight_capacity` per backend WILL break MLX (it
serializes internally).

### 3. Are pinned prompts still warm?

```bash
tail -50 ~/.cache/forex_trading/logs/serving_gateway.log | grep -i "warm"
```

You should see periodic `[warmer] pinned prompt warmed: validator-v1-canonical`
lines every ~180 s. If they stopped, the refresher task crashed —
restart the gateway, don't revert.

### 4. Is one tenant starving another?

```bash
curl -s http://127.0.0.1:11503/metrics | grep requests_by_tenant
```

If `background` is winning the queue over `trading`, the tenant
config is misapplied — fix `config.yaml` priorities and restart.
Don't revert.

## Per-commit revert (if you need surgical rollback)

You usually want all-or-nothing. But if one commit is suspect, you
can revert just that one. They're independent.

| Commit | What it does | Revert effect |
|---|---|---|
| `05db7f1e` | Ghost callers → gateway, `background` tenant | Ghost recording goes back to `:11502` direct. Live trading unaffected (different code paths). |
| `64ca8618` | 5 trading helpers → gateway, `trading` tenant | Validator narrator, snipe cleanup, news, floor chat, intel synthesis go back to `:11502` direct. Trading flow restored even if gateway is dead. |
| `4dd89d1e` | Swarm `mlx/CSO` → gateway | Swarm-routed agents (validator, TA, guardian narrator, etc. *that go through swarm dispatch*) go back to `:11502` direct. |
| `14a89c9f` | Launcher starts gateway alongside MLX | Gateway no longer starts at boot. Safe to leave in place — if no callers route to `:11503`, nothing notices. |

```bash
git revert <commit>
# resolve any minor merge fuzz, no manual changes needed
./trading_launcher.sh restart
```

### Reverting only the trading helpers (most common partial revert)

If the swarm path is fine but a specific helper is broken:

```bash
git revert 64ca8618
```

That brings back the 5 helpers' direct `:11502` URLs and drops the
`X-Jarvis-Tenant: trading` headers. The gateway keeps running, swarm
CSO keeps using it.

## After rollback — verify

```bash
# 1. MLX is reachable directly
curl -s http://127.0.0.1:11502/v1/models | jq '.data[0].id'
# expect: "mlx-community/Qwen3.5-35B-A3B-4bit"

# 2. A trading helper actually calls something
source ~/myenv/bin/activate
cd "<repo_root>/Source"
python -c "
from guardian_narrator import _call_local_agent
out = _call_local_agent('You are a tester.', 'Reply: OK', max_tokens=4)
print('reply:', repr(out))
"
# expect: reply: 'OK' (or similar)

# 3. Tail the trading log for at least one full cycle (15 min on M15)
tail -f "<repo_root>/Source/logs/trading_cycle.log"
# look for VALIDATOR_VERDICT and GUARDIAN_THREAT events landing within
# normal latencies (validator < 60 s, guardian < 5 s)
```

If any of those fail, the rollback didn't fully take. Check
`git log --oneline -10` to confirm the revert commits are on HEAD.

## Re-deploying after a fix

Once the gateway bug is fixed:

1. Land the fix on a branch, run `pytest Source/serving/test_gateway.py`
2. Re-cherry-pick (or re-merge) the reverted commits in the original
   order: `4dd89d1e`, `64ca8618`, `05db7f1e`
3. `./trading_launcher.sh restart`
4. Watch `/metrics` for one full cycle before walking away

Don't re-deploy mid-session. Wait for a quiet window.

## Files in scope (for grep)

- `Source/serving/__init__.py`
- `Source/serving/backend.py`
- `Source/serving/config.yaml`
- `Source/serving/gateway.py`
- `Source/serving/pinned_prompts.py`
- `Source/serving/request_queue.py`
- `Source/serving/run_gateway.py`
- `Source/serving/tenants.py`
- `Source/serving/test_gateway.py`
- `Source/trading_launcher.sh` (gateway start/stop/status block)
- `Handler/handler_swarm.py:1046` (`MLX_SERVERS["CSO"]["port"]`)
- `Forex Trading Team/Source/guardian_narrator.py`
- `Forex Trading Team/Source/snipe_cleanup.py`
- `Forex Trading Team/Source/news_sentiment_scorer.py`
- `Forex Trading Team/Source/floor_chat.py`
- `Forex Trading Team/Source/intelligence_agent_prep.py`
- `Forex Trading Team/Source/agents/trading_cycle.py` (lines 1442, 1475, 6920)
- `Forex Trading Team/Source/optimizer/ghost_replay.py`

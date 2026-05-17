# OANDA MCP — Complete API Reference

> **Handler:** `handler_oanda` (OandaHandler)
> **API:** OANDA v20 REST API
> **Base URLs:**
> - Practice REST: `https://api-fxpractice.oanda.com`
> - Live REST: `https://api-fxtrade.oanda.com`
> - Practice Stream: `https://stream-fxpractice.oanda.com`
> - Live Stream: `https://stream-fxtrade.oanda.com`
> **Rate Limits:** 100 requests/second, 2 new connections/second
> **Auth:** Bearer token (API key loaded from `API/OANDA_API_KEY.txt` or `OANDA_API_KEY` env var)
> **Default Account:** `101-001-24637237-001` (practice, $101K balance)
> **DateTime Format:** RFC3339 (e.g. `2026-02-17T12:00:00.000000000Z`)

---

## 1. ACCOUNT ENDPOINTS

### 1.1 `list_accounts()`
List all accounts authorized for the API token.

**Parameters:** None

**Response:**
```json
{
  "accounts": [
    {"id": "101-001-24637237-001", "tags": []}
  ]
}
```

**Use case:** Discovery — verify which accounts the token can access.

---

### 1.2 `get_account(account_id=None)`
Full account details including all open trades, pending orders, and positions.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `account_id` | str | default account | Account to query |

**Response:** Full account object with nested:
- `account.balance` — current balance
- `account.NAV` — net asset value (balance + unrealized P&L)
- `account.unrealizedPL` — total unrealized P&L
- `account.marginUsed` / `account.marginAvailable`
- `account.openTradeCount` / `account.openPositionCount` / `account.pendingOrderCount`
- `account.trades[]` — array of all open trade objects (full detail)
- `account.orders[]` — array of all pending order objects
- `account.positions[]` — array of all position objects
- `account.lastTransactionID` — latest transaction ID (use for polling)

**Use case:** Full account snapshot. Heavy response — use `get_account_summary` for lightweight checks.

---

### 1.3 `get_account_summary(account_id=None)`
Lightweight account summary — balances, margin, P&L, trade counts.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `account_id` | str | default account | Account to query |

**Response:**
```json
{
  "account": {
    "id": "101-001-24637237-001",
    "currency": "USD",
    "balance": "101000.0000",
    "pl": "-500.0000",
    "unrealizedPL": "125.3400",
    "NAV": "101125.3400",
    "marginUsed": "2500.0000",
    "marginAvailable": "98625.3400",
    "positionValue": "50000.0000",
    "openTradeCount": 2,
    "openPositionCount": 2,
    "pendingOrderCount": 1,
    "financing": "-12.5000",
    "lastTransactionID": "6789"
  }
}
```

**Key fields for trading decisions:**
- `balance` — base capital (no open trade P&L)
- `NAV` — balance + unrealizedPL (true account value)
- `marginAvailable` — how much margin is free for new trades
- `openTradeCount` — number of active trades
- `lastTransactionID` — bookmark for `get_account_changes()` polling

---

### 1.4 `get_account_instruments(account_id=None, instruments=None)`
List tradeable instruments and their specifications.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `account_id` | str | default account | Account to query |
| `instruments` | str | all | CSV list of instrument names to filter |

**Response per instrument:**
```json
{
  "name": "EUR_USD",
  "type": "CURRENCY",
  "displayName": "EUR/USD",
  "pipLocation": -4,
  "displayPrecision": 5,
  "tradeUnitsPrecision": 0,
  "minimumTradeSize": "1",
  "maximumTrailingStopDistance": "1.00000",
  "minimumTrailingStopDistance": "0.00050",
  "maximumPositionSize": "0",
  "maximumOrderUnits": "100000000",
  "marginRate": "0.05",
  "guaranteedStopLossOrderMode": "DISABLED",
  "financing": { ... }
}
```

**Critical fields:**
- `pipLocation` — where the pip is (-4 = 4th decimal for EUR_USD, -2 = 2nd decimal for USD_JPY)
- `displayPrecision` — decimal places for price display
- `minimumTradeSize` — minimum units per trade (usually "1")
- `marginRate` — margin requirement (0.05 = 5% = 20:1 leverage)
- `maximumOrderUnits` — max units per single order
- `minimumTrailingStopDistance` — closest trailing stop allowed

**Use case:** Get pip sizes for P&L calculation, margin requirements for position sizing, min/max trade sizes.

---

### 1.5 `set_account_configuration(alias=None, margin_rate=None, account_id=None)`
Set account alias or default margin rate.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `alias` | str | None | Human-readable account name |
| `margin_rate` | str | None | Default margin rate (e.g. "0.05" for 20:1) |
| `account_id` | str | default account | Account to configure |

**Use case:** Administrative — set account nickname, adjust default leverage.

---

### 1.6 `get_account_changes(since_transaction_id, account_id=None)`
Poll for all account changes since a specific transaction ID. Returns new/modified/closed trades, orders, positions, and transactions.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `since_transaction_id` | str | **required** | Transaction ID to poll from (exclusive) |
| `account_id` | str | default account | Account to poll |

**Response:**
```json
{
  "changes": {
    "tradesOpened": [...],
    "tradesReduced": [...],
    "tradesClosed": [...],
    "ordersFilled": [...],
    "ordersCreated": [...],
    "ordersCancelled": [...],
    "positionsChanged": [...]
  },
  "state": { ... },
  "lastTransactionID": "6795"
}
```

**Use case:** Efficient polling loop — instead of repeatedly calling `get_account`, call this with the `lastTransactionID` from your previous call to get only what changed. Essential for:
- Detecting when a trade was closed by SL/TP
- Detecting when pending orders fill
- Detecting external account activity
- Building a transaction-based event loop without streaming

**Polling pattern:**
1. Call `get_account_summary()` → save `lastTransactionID`
2. Every N seconds: `get_account_changes(since_transaction_id=saved_id)`
3. Process changes, update saved_id to response's `lastTransactionID`
4. If `lastTransactionID` == saved_id → no changes occurred

---

## 2. INSTRUMENT / CANDLE ENDPOINTS

### 2.1 `get_candles(instrument, price="M", granularity="H1", count=500, from_time=None, to_time=None, smooth=None, include_first=None, daily_alignment=None, alignment_timezone=None, weekly_alignment=None)`
Fetch candlestick data for any instrument. The primary data retrieval endpoint.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `instrument` | str | **required** | e.g. "EUR_USD", "USD_JPY" |
| `price` | str | "M" | Price component: "M" (mid), "B" (bid), "A" (ask), or combinations "MBA" |
| `granularity` | str | "H1" | Timeframe (see full list below) |
| `count` | int | 500 | Number of candles (max 5000) |
| `from_time` | str | None | Start time (RFC3339). Mutually exclusive with `count` when used with `to_time` |
| `to_time` | str | None | End time (RFC3339) |
| `smooth` | bool | None | Smooth candles (fill gaps) |
| `include_first` | bool | None | Include first candle that contains `from_time` |
| `daily_alignment` | int | None | Hour (0-23) for daily candle alignment |
| `alignment_timezone` | str | None | Timezone for alignment (e.g. "America/New_York") |
| `weekly_alignment` | str | None | Day for weekly alignment ("Monday", "Friday", etc.) |

**Supported Granularities:**
| Category | Values | Description |
|----------|--------|-------------|
| Seconds | S5, S10, S15, S30 | 5/10/15/30-second candles |
| Minutes | M1, M2, M4, M5, M10, M15, M30 | Minute candles |
| Hours | H1, H2, H3, H4, H6, H8, H12 | Hour candles |
| Day+ | D, W, M | Daily, Weekly, Monthly |

**Response:**
```json
{
  "instrument": "EUR_USD",
  "granularity": "H1",
  "candles": [
    {
      "complete": true,
      "volume": 1234,
      "time": "2026-02-17T10:00:00.000000000Z",
      "mid": {
        "o": "1.04850",
        "h": "1.04920",
        "l": "1.04800",
        "c": "1.04890"
      }
    }
  ]
}
```

**Key behaviors:**
- `complete: true` means the candle is finalized. The last candle in a request is often `complete: false` (still forming)
- `price="M"` returns `mid` object. `price="BA"` returns both `bid` and `ask` objects
- `count` and `from_time`+`to_time` are alternative ways to specify range. Use `count` for "latest N candles", use time range for historical data
- Max 5000 candles per request. For longer history, paginate with `from_time`/`to_time`
- Empty candles (no trading activity) may be omitted unless `smooth=true`

**Data quality notes:**
- Always check `complete` flag — incomplete candles should not be used for indicator calculation
- Volume is tick volume, not dollar volume
- Weekend gaps are normal — no candles from Fri 5pm ET to Sun 5pm ET

---

### 2.2 `get_account_candles(instrument, price="M", granularity="H1", count=500, from_time=None, to_time=None, units=None, account_id=None)`
Fetch candles through the account endpoint. Adds volume-weighting based on trade units.

**Parameters:** Same as `get_candles` plus:
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `units` | str | None | Units for volume-weighting |

**Use case:** When you need volume-weighted candles relative to your position size. Rarely needed — `get_candles` is sufficient for most analysis.

---

### 2.3 `get_latest_candles(candle_specifications, units=None, smooth=None, account_id=None)`
Get the latest (most recent) candle for multiple instrument/granularity combinations in a single call.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `candle_specifications` | str | **required** | Colon-separated specs, comma-delimited: `"EUR_USD:H1:M,USD_JPY:M15:BA"` |
| `units` | str | None | Volume-weighting units |
| `smooth` | bool | None | Smooth candles |

**Spec format:** `{instrument}:{granularity}:{price}` — e.g. `EUR_USD:S5:BM`

**Use case:** Quick snapshot of current candle across multiple pairs/timeframes without fetching full history. Efficient for dashboard updates or quick spread checks.

---

## 3. ORDER ENDPOINTS

### 3.1 `create_order(order, account_id=None)`
Create any order type using a raw order specification dictionary. This is the low-level endpoint — use the convenience methods below for common order types.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `order` | dict | **required** | Full order specification |
| `account_id` | str | default account | Account to place order in |

**Order types supported:** MARKET, LIMIT, STOP, MARKET_IF_TOUCHED, TAKE_PROFIT, STOP_LOSS, TRAILING_STOP_LOSS, GUARANTEED_STOP_LOSS

---

### 3.2 `place_market_order(instrument, units, stop_loss_price=None, take_profit_price=None, trailing_stop_distance=None, time_in_force="FOK", client_extensions=None, account_id=None)`
Place an immediate market order.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `instrument` | str | **required** | e.g. "EUR_USD" |
| `units` | int | **required** | Positive = buy, negative = sell |
| `stop_loss_price` | str | None | SL price (e.g. "1.04500") |
| `take_profit_price` | str | None | TP price (e.g. "1.05500") |
| `trailing_stop_distance` | str | None | Trailing SL distance in price units (e.g. "0.00100" = 10 pips for EUR_USD) |
| `time_in_force` | str | "FOK" | "FOK" (fill-or-kill) or "IOC" (immediate-or-cancel) |
| `client_extensions` | dict | None | `{"id": "my_trade_1", "tag": "setup_S15", "comment": "divergence signal"}` |
| `account_id` | str | default account | Account to trade in |

**Response (success):**
```json
{
  "orderCreateTransaction": {
    "id": "6790",
    "type": "MARKET_ORDER",
    "instrument": "EUR_USD",
    "units": "10000",
    "timeInForce": "FOK"
  },
  "orderFillTransaction": {
    "id": "6791",
    "type": "ORDER_FILL",
    "instrument": "EUR_USD",
    "units": "10000",
    "price": "1.04875",
    "pl": "0.0000",
    "financing": "0.0000",
    "tradeOpened": {
      "tradeID": "6791",
      "units": "10000",
      "price": "1.04875",
      "initialMarginRequired": "525.0000"
    }
  },
  "relatedTransactionIDs": ["6790", "6791"]
}
```

**Response (rejected):**
```json
{
  "orderCreateTransaction": { ... },
  "orderRejectTransaction": {
    "type": "MARKET_ORDER_REJECT",
    "rejectReason": "INSUFFICIENT_MARGIN"
  }
}
```

**Key behaviors:**
- `units` is always an integer. Positive = buy (long), negative = sell (short)
- SL/TP are set as "on fill" orders — they're created automatically when the market order fills
- `client_extensions.id` must be unique across all trades/orders in the account
- `client_extensions.tag` useful for tagging which setup triggered the trade
- FOK = must fill entire order or cancel. IOC = fill what you can, cancel the rest

---

### 3.3 `place_limit_order(instrument, units, price, stop_loss_price=None, take_profit_price=None, trailing_stop_distance=None, time_in_force="GTC", client_extensions=None, account_id=None)`
Place a limit order at a specific price. Fills when market reaches the price.

**Parameters:** Same structure as market order, plus:
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `price` | str | **required** | Limit price to fill at |
| `time_in_force` | str | "GTC" | "GTC" (good-til-cancelled), "GTD" (good-til-date), "GFD" (good-for-day) |

**Use case:** Entry at a better price. Example: current EUR_USD = 1.0490, place limit buy at 1.0470 (wait for pullback).

---

### 3.4 `place_stop_order(instrument, units, price, ...)`
Place a stop order — triggers when price moves through the specified level.

**Parameters:** Same as limit order.

**Use case:** Breakout entries. Example: EUR_USD consolidating at 1.0490, place stop buy at 1.0520 (triggers on breakout above resistance).

---

### 3.5 `place_market_if_touched_order(instrument, units, price, ...)`
Place a market-if-touched order — becomes a market order when price touches the level.

**Parameters:** Same as limit order.

**Difference from limit:** Limit guarantees the fill price. Market-if-touched triggers a market order at whatever price is available, which could be worse than the trigger price in fast markets.

---

### 3.6 `place_take_profit_order(trade_id, price, time_in_force="GTC", account_id=None)`
Attach a take-profit order to an existing open trade.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `trade_id` | str | **required** | Trade ID to attach TP to |
| `price` | str | **required** | TP price level |
| `time_in_force` | str | "GTC" | Usually "GTC" |

**Use case:** Add or modify TP on an existing trade. Use after partial exits to set new TP on remaining units.

---

### 3.7 `place_stop_loss_order(trade_id, price, time_in_force="GTC", account_id=None)`
Attach a stop-loss order to an existing open trade.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `trade_id` | str | **required** | Trade ID to attach SL to |
| `price` | str | **required** | SL price level |
| `time_in_force` | str | "GTC" | Usually "GTC" |

**Use case:** Add or move SL. Critical for: moving SL to breakeven after partial profit, tightening SL as trade moves in your favor.

---

### 3.8 `place_trailing_stop_loss_order(trade_id, distance, time_in_force="GTC", account_id=None)`
Attach a trailing stop-loss to an existing trade.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `trade_id` | str | **required** | Trade ID |
| `distance` | str | **required** | Trail distance in price units (e.g. "0.00150" = 15 pips EUR_USD) |
| `time_in_force` | str | "GTC" | Usually "GTC" |

**Behavior:** The SL moves with price in the favorable direction but never moves backward. If price reverses by the distance amount, the trade closes.

**Use case:** Lock in profits on runners. Set after price has moved 1:1 in your favor.

---

### 3.9 `place_guaranteed_stop_loss_order(trade_id, price, time_in_force="GTC", account_id=None)`
Attach a guaranteed stop-loss (no slippage) to a trade. OANDA charges a premium for this.

**Parameters:** Same as `place_stop_loss_order`.

**Note:** Check `guaranteedStopLossOrderMode` in instrument specs — may be "DISABLED" for some instruments/accounts.

---

### 3.10 `list_orders(account_id=None, ids=None, state=None, instrument=None, count=None, before_id=None)`
List orders with filtering.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `ids` | str | None | CSV list of order IDs to retrieve |
| `state` | str | None | "PENDING", "FILLED", "TRIGGERED", "CANCELLED", "ALL" |
| `instrument` | str | None | Filter by instrument |
| `count` | int | None | Max results |
| `before_id` | str | None | Pagination — orders before this ID |

---

### 3.11 `list_pending_orders(account_id=None)`
List all currently pending (unfilled) orders.

**Use case:** Check what limit/stop orders are waiting. Important before placing new orders to avoid duplicates.

---

### 3.12 `get_order(order_specifier, account_id=None)`
Get details of a specific order.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `order_specifier` | str | **required** | Order ID or client order ID (prefixed with "@", e.g. "@my_order") |

---

### 3.13 `replace_order(order_specifier, order, account_id=None)`
Replace (cancel + recreate) an existing pending order with new specifications.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `order_specifier` | str | **required** | Order to replace |
| `order` | dict | **required** | New order specification |

**Use case:** Adjust a pending limit/stop order's price or size without manually cancelling and recreating.

---

### 3.14 `cancel_order(order_specifier, account_id=None)`
Cancel a pending order.

---

### 3.15 `set_order_client_extensions(order_specifier, client_extensions=None, trade_client_extensions=None, account_id=None)`
Set or modify client extensions on an order. `trade_client_extensions` apply to the trade created when the order fills.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `client_extensions` | dict | None | `{"id": "...", "tag": "...", "comment": "..."}` for the order |
| `trade_client_extensions` | dict | None | `{"id": "...", "tag": "...", "comment": "..."}` for the resulting trade |

---

## 4. TRADE ENDPOINTS

### 4.1 `list_trades(account_id=None, ids=None, state=None, instrument=None, count=None, before_id=None)`
List trades with filtering.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `ids` | str | None | CSV list of trade IDs |
| `state` | str | None | "OPEN", "CLOSED", "CLOSE_WHEN_TRADEABLE", "ALL" |
| `instrument` | str | None | Filter by instrument |
| `count` | int | None | Max results (default 50, max 500) |
| `before_id` | str | None | Pagination |

**Use case:** Query trade history. `state="CLOSED"` gives you completed trades for P&L analysis.

---

### 4.2 `list_open_trades(account_id=None)`
List all currently open trades.

**Response:**
```json
{
  "trades": [
    {
      "id": "6791",
      "instrument": "EUR_USD",
      "price": "1.04875",
      "openTime": "2026-02-17T10:30:00.000Z",
      "initialUnits": "10000",
      "currentUnits": "10000",
      "state": "OPEN",
      "unrealizedPL": "12.5000",
      "marginUsed": "525.0000",
      "stopLossOrder": {
        "id": "6792",
        "price": "1.04500",
        "timeInForce": "GTC"
      },
      "takeProfitOrder": {
        "id": "6793",
        "price": "1.05500",
        "timeInForce": "GTC"
      },
      "clientExtensions": {
        "id": "cycle_1_EUR_USD",
        "tag": "confluence_78",
        "comment": "Risk profile: default"
      }
    }
  ]
}
```

**Key fields:**
- `currentUnits` — may differ from `initialUnits` after partial closes
- `unrealizedPL` — current floating P&L
- `stopLossOrder` / `takeProfitOrder` / `trailingStopLossOrder` — attached dependent orders
- `clientExtensions` — your tags for tracking which cycle/setup opened this trade

---

### 4.3 `get_trade(trade_specifier, account_id=None)`
Get full details of a specific trade.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `trade_specifier` | str | **required** | Trade ID or client trade ID ("@my_trade") |

---

### 4.4 `close_trade(trade_specifier, units="ALL", account_id=None)`
Close a trade fully or partially.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `trade_specifier` | str | **required** | Trade ID |
| `units` | str | "ALL" | "ALL" to close entirely, or a number (e.g. "5000") for partial close |

**Response:**
```json
{
  "orderFillTransaction": {
    "type": "ORDER_FILL",
    "instrument": "EUR_USD",
    "units": "-10000",
    "price": "1.04950",
    "pl": "7.5000",
    "financing": "-0.2500",
    "tradesClosed": [
      {
        "tradeID": "6791",
        "units": "-10000",
        "realizedPL": "7.5000"
      }
    ]
  }
}
```

**Partial close behavior:**
- Close "5000" of a 10000-unit trade → `currentUnits` becomes 5000
- SL/TP orders remain attached to the remaining position
- `realizedPL` in the response = P&L on the closed portion
- Trade ID stays the same — it's the same trade, just smaller

**Use case:** Partial exits — take 50% profit at 1:1 R:R, let the rest run.

---

### 4.5 `set_trade_client_extensions(trade_specifier, client_extensions, account_id=None)`
Modify client extensions on an open trade.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `trade_specifier` | str | **required** | Trade ID |
| `client_extensions` | dict | **required** | `{"id": "...", "tag": "...", "comment": "..."}` |

**Use case:** Update tracking tags mid-trade (e.g., tag with "partial_exit_done" after taking profit).

---

### 4.6 `set_trade_dependent_orders(trade_specifier, take_profit=None, stop_loss=None, trailing_stop_loss=None, guaranteed_stop_loss=None, account_id=None)`
Set, modify, or cancel TP/SL/TSL/GSL on an open trade in a single call.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `trade_specifier` | str | **required** | Trade ID |
| `take_profit` | dict | None | `{"price": "1.05500"}` to set/modify, `{"price": "0"}` or omit to cancel |
| `stop_loss` | dict | None | `{"price": "1.04500"}` or `{"price": "1.04875"}` (move to breakeven) |
| `trailing_stop_loss` | dict | None | `{"distance": "0.00150"}` |
| `guaranteed_stop_loss` | dict | None | `{"price": "1.04400"}` |

**Use case:** This is the single most important endpoint for trade management:
- **Move SL to breakeven:** `stop_loss={"price": "1.04875"}` (entry price)
- **Tighten SL:** `stop_loss={"price": "1.04950"}` (lock in profit)
- **Switch from fixed SL to trailing:** `stop_loss=None, trailing_stop_loss={"distance": "0.00150"}`
- **Adjust TP after partial exit:** `take_profit={"price": "1.06000"}`
- **Remove TP (let it run):** `take_profit={"price": "0"}` or set to very far level
- **Multiple changes at once:** Set both new SL and new TP in one call

---

## 5. POSITION ENDPOINTS

### 5.1 `list_positions(account_id=None)`
List all positions including instruments with no current exposure.

---

### 5.2 `list_open_positions(account_id=None)`
List only positions with active exposure (at least one open trade).

**Response:**
```json
{
  "positions": [
    {
      "instrument": "EUR_USD",
      "pl": "-45.0000",
      "unrealizedPL": "12.5000",
      "long": {
        "units": "10000",
        "averagePrice": "1.04875",
        "pl": "-45.0000",
        "unrealizedPL": "12.5000",
        "tradeIDs": ["6791"]
      },
      "short": {
        "units": "0"
      }
    }
  ]
}
```

**Use case:** Quick overview of all open exposure. The `long` and `short` sub-objects show the net position per side.

---

### 5.3 `get_position(instrument, account_id=None)`
Get position detail for a specific instrument.

---

### 5.4 `close_position(instrument, long_units=None, short_units=None, account_id=None)`
Close a position by instrument (instead of by trade ID).

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `instrument` | str | **required** | e.g. "EUR_USD" |
| `long_units` | str | None | "ALL" or a number to close long exposure |
| `short_units` | str | None | "ALL" or a number to close short exposure |

**Use case:** Close all exposure on an instrument without knowing individual trade IDs. Good for emergency exits — "close everything on EUR_USD."

---

## 6. PRICING ENDPOINTS

### 6.1 `get_pricing(instruments, since=None, include_home_conversions=None, account_id=None)`
Get current bid/ask pricing for one or more instruments.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `instruments` | str | **required** | CSV list: "EUR_USD,USD_JPY,GBP_USD" |
| `since` | str | None | Only return prices changed since this time (RFC3339) |
| `include_home_conversions` | bool | None | Include conversion factors to account currency |

**Response:**
```json
{
  "prices": [
    {
      "instrument": "EUR_USD",
      "time": "2026-02-17T10:30:15.000000000Z",
      "tradeable": true,
      "bids": [
        {"price": "1.04870", "liquidity": 10000000}
      ],
      "asks": [
        {"price": "1.04885", "liquidity": 10000000}
      ],
      "closeoutBid": "1.04870",
      "closeoutAsk": "1.04885",
      "status": "tradeable"
    }
  ]
}
```

**Key fields:**
- `tradeable` — false during market close or halts
- `bids[0].price` — best bid (price you sell at)
- `asks[0].price` — best ask (price you buy at)
- Spread = ask - bid (e.g. 1.04885 - 1.04870 = 0.00015 = 1.5 pips)
- Multiple liquidity levels may be present (depth of book)
- `closeoutBid`/`closeoutAsk` — prices used for margin closeout calculations

**Spread awareness:**
| Pair | Normal Spread | Wide (warning) | Extreme (danger) |
|------|--------------|----------------|-------------------|
| EUR_USD | 0.8-1.5 pips | >3 pips | >5 pips |
| USD_JPY | 0.8-1.5 pips | >3 pips | >5 pips |
| GBP_USD | 1.0-2.0 pips | >4 pips | >6 pips |
| EUR_JPY | 1.5-2.5 pips | >5 pips | >8 pips |

Wide spreads indicate: low liquidity, market close approaching, news event imminent, or weekend gap.

---

### 6.2 `get_pricing_stream_url(instruments, account_id=None)`
Build the URL for OANDA's live pricing stream.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `instruments` | str | **required** | CSV list of instruments to stream |

**Returns:** URL string (not a connection). Connect to this URL with a streaming HTTP client.

**Stream format (line-delimited JSON):**
```json
{"type": "PRICE", "instrument": "EUR_USD", "time": "2026-02-17T10:30:15.123Z", "bids": [{"price": "1.04870"}], "asks": [{"price": "1.04885"}], "tradeable": true}
{"type": "HEARTBEAT", "time": "2026-02-17T10:30:20.000Z"}
```

**Stream behaviors:**
- Prices arrive tick-by-tick (every price change)
- Heartbeat messages every ~5 seconds when no price changes
- Connection stays open indefinitely — must handle reconnection on disconnect
- Each line is a complete JSON object (newline-delimited)
- `type=PRICE` = new price, `type=HEARTBEAT` = keep-alive

**Use cases:**
- Real-time spread monitoring during active trades
- Detecting when spread widens (news event, liquidity drop)
- Triggering position management rules based on live price
- Latency-sensitive entry timing
- Dashboard live price feed

---

## 7. TRANSACTION ENDPOINTS

### 7.1 `list_transactions(from_time=None, to_time=None, page_size=None, type_filter=None, account_id=None)`
List transaction page URLs in a time range.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_time` | str | None | Start time (RFC3339) |
| `to_time` | str | None | End time (RFC3339) |
| `page_size` | int | None | Results per page (max 1000) |
| `type_filter` | str | None | CSV of transaction types to filter |

**Transaction types:** ORDER_FILL, ORDER_CANCEL, STOP_LOSS_ORDER, TAKE_PROFIT_ORDER, TRAILING_STOP_LOSS_ORDER, MARKET_ORDER, LIMIT_ORDER, DAILY_FINANCING, CLIENT_CONFIGURE, TRANSFER_FUNDS, etc.

**Use case:** Historical audit trail. "Show me all ORDER_FILL transactions from last week."

---

### 7.2 `get_transaction(transaction_id, account_id=None)`
Get full details of a single transaction.

---

### 7.3 `get_transactions_id_range(from_id, to_id, type_filter=None, account_id=None)`
Get all transactions in an ID range.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_id` | str | **required** | Start transaction ID (inclusive) |
| `to_id` | str | **required** | End transaction ID (inclusive) |
| `type_filter` | str | None | CSV of transaction types |

---

### 7.4 `get_transactions_since_id(since_id, type_filter=None, account_id=None)`
Get all transactions since a specific ID (exclusive).

**Use case:** Catch up on what happened since you last checked. Pair with `get_account_changes` for a complete picture of account activity.

---

### 7.5 `get_transaction_stream_url(account_id=None)`
Build the URL for OANDA's live transaction stream.

**Returns:** URL string for streaming HTTP connection.

**Stream format (line-delimited JSON):**
```json
{"type": "TRANSACTION", "id": "6795", "time": "...", "type": "ORDER_FILL", "instrument": "EUR_USD", ...}
{"type": "HEARTBEAT", "time": "..."}
```

**Use cases:**
- Real-time notification when a trade is opened/closed/modified
- Detecting when SL/TP orders fire
- Building an event-driven system — react to account events as they happen
- Audit logging of all account activity in real-time

---

## 8. UTILITY ENDPOINTS

### 8.1 `get_available_actions()`
List all available handler actions (method names).

### 8.2 `get_supported_granularities()`
Returns the full list of supported candle granularities.

### 8.3 `ping()`
Test connectivity. Returns account balance, NAV, currency if successful, error message if not.

**Response (success):**
```json
{
  "status": "ok",
  "account_id": "101-001-24637237-001",
  "practice": true,
  "balance": "101000.0000",
  "nav": "101125.3400",
  "currency": "USD"
}
```

---

## 9. STREAMING ARCHITECTURE

OANDA provides two streaming endpoints. Both use long-lived HTTP connections with line-delimited JSON.

### 9.1 Pricing Stream
**URL:** `get_pricing_stream_url(instruments="EUR_USD,GBP_USD,USD_JPY")`
**Base:** `https://stream-fxpractice.oanda.com/v3/accounts/{id}/pricing/stream`

**What it delivers:**
- Every tick (price change) for subscribed instruments
- Heartbeats every ~5 seconds when no ticks
- Bid/ask with liquidity, tradeable flag, timestamp

**Connection pattern:**
```python
import requests

url = handler.get_pricing_stream_url("EUR_USD,GBP_USD")
headers = {"Authorization": f"Bearer {api_key}"}

with requests.get(url, headers=headers, stream=True) as resp:
    for line in resp.iter_lines():
        if line:
            data = json.loads(line)
            if data["type"] == "PRICE":
                # Process tick: instrument, bid, ask, spread, tradeable
                pass
            elif data["type"] == "HEARTBEAT":
                # Connection alive, no price change
                pass
```

**Reconnection strategy:**
- If disconnected, wait 1 second, reconnect
- Back off exponentially on repeated failures (1s, 2s, 4s, 8s... max 60s)
- Log disconnect/reconnect events for monitoring

### 9.2 Transaction Stream
**URL:** `get_transaction_stream_url()`
**Base:** `https://stream-fxpractice.oanda.com/v3/accounts/{id}/transactions/stream`

**What it delivers:**
- Every transaction: order fills, SL/TP triggers, order cancellations, daily financing
- Heartbeats when no activity

**Use together:**
- Pricing stream → feed position management rules (spread monitoring, trailing stop logic)
- Transaction stream → detect when trades close (SL/TP hit), update trade log, trigger reporting

---

## 10. RESPONSE PATTERNS & ERROR HANDLING

### Common error codes:
| Status | Meaning | Action |
|--------|---------|--------|
| 400 | Bad request (invalid params) | Fix the request parameters |
| 401 | Unauthorized (bad API key) | Check API key |
| 403 | Forbidden (account not allowed) | Check account permissions |
| 404 | Not found (bad trade/order ID) | Verify the ID exists |
| 405 | Method not allowed | Check HTTP method |
| 429 | Rate limited | Back off, retry after delay |

### Rate limit handling:
The handler includes a built-in rate limiter (100 req/s). If you hit 429:
1. The built-in limiter should prevent this in normal operation
2. If hit externally: wait 1 second, retry
3. Monitor `_timestamps` list length in logs

### Units as strings:
OANDA returns all numeric values as **strings**, not floats. Always convert:
```python
balance = float(account["balance"])  # "101000.0000" → 101000.0
```

### Client extensions pattern:
Every order and trade supports client extensions for tracking:
```json
{
  "id": "cycle_42_EUR_USD_buy",
  "tag": "setup_S15_divergence",
  "comment": "confluence=78, regime=ranging, session=london_ny"
}
```
- `id` must be unique across all active orders/trades
- `tag` and `comment` are free-form (max 128 chars each)
- Set on order → inherited by trade when order fills
- Query by client ID using "@" prefix: `get_trade("@cycle_42_EUR_USD_buy")`

---

## 11. INSTRUMENT REFERENCE

### 13 Traded Pairs:
| Pair | Pip Location | Display Precision | Normal Spread | Type |
|------|-------------|-------------------|---------------|------|
| EUR_USD | -4 | 5 | 0.8-1.5 pips | Major |
| USD_JPY | -2 | 3 | 0.8-1.5 pips | Major |
| GBP_USD | -4 | 5 | 1.0-2.0 pips | Major |
| AUD_USD | -4 | 5 | 1.0-1.8 pips | Major |
| NZD_USD | -4 | 5 | 1.5-2.5 pips | Major |
| USD_CAD | -4 | 5 | 1.2-2.0 pips | Major |
| USD_CHF | -4 | 5 | 1.2-2.0 pips | Major |
| EUR_GBP | -4 | 5 | 1.0-2.0 pips | Cross |
| EUR_JPY | -2 | 3 | 1.5-2.5 pips | Cross |
| GBP_JPY | -2 | 3 | 2.0-3.5 pips | Cross |
| AUD_NZD | -4 | 5 | 2.0-3.5 pips | Cross |
| EUR_CHF | -4 | 5 | 1.5-2.5 pips | Cross |
| EUR_AUD | -4 | 5 | 2.0-3.5 pips | Cross |

### Pip calculation:
- **-4 pairs** (EUR_USD, GBP_USD, etc.): 1 pip = 0.0001. Price 1.04875 → 5th decimal is a pipette (0.1 pip)
- **-2 pairs** (USD_JPY, EUR_JPY, GBP_JPY): 1 pip = 0.01. Price 152.345 → 3rd decimal is a pipette

### Pip value (for 1 standard lot = 100,000 units):
- XXX_USD pairs: 1 pip = $10.00
- USD_XXX pairs: 1 pip = $10.00 / current price × 100,000
- Cross pairs: convert through USD

---

## 12. CONFIRMED LIVE BEHAVIORS

All endpoints confirmed working against practice account (Feb 2026):

- `get_candles("EUR_USD", granularity="H1", count=250)` → 250 candles, ~0.3s
- `get_account_summary()` → balance $101K, 0 open trades, ~0.2s
- `get_pricing("EUR_USD")` → bid/ask with liquidity levels, ~0.2s
- `get_account_instruments(instruments="EUR_USD")` → full spec including pip location, margin rate
- `place_market_order("EUR_USD", 1000)` → fills immediately, returns trade ID
- `close_trade("6791")` → closes with realized P&L
- `set_trade_dependent_orders("6791", stop_loss={"price": "1.04500"})` → SL attached
- `get_pricing_stream_url("EUR_USD")` → returns valid URL
- `get_transaction_stream_url()` → returns valid URL
- `get_account_changes(since_transaction_id="6789")` → returns changes since that txn
- 68 instruments total available on practice account

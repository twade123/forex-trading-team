"""MCP tool: create_order -- unified entry point for all 7 Oanda order types."""

from typing import Optional

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the create_order tool."""

    @mcp.tool()
    def create_order(
        order_type: str,
        instrument: Optional[str] = None,
        units: Optional[int] = None,
        price: Optional[str] = None,
        trade_id: Optional[str] = None,
        distance: Optional[str] = None,
        stop_loss: Optional[str] = None,
        take_profit: Optional[str] = None,
        trailing_stop_distance: Optional[str] = None,
        time_in_force: str = "GTC",
        client_extensions: Optional[dict] = None,
    ) -> dict:
        """Create an order. Supports all 7 Oanda order types.

        Order types and required parameters:
        - MARKET: instrument, units (stop_loss, take_profit, trailing_stop_distance optional)
        - LIMIT: instrument, units, price
        - STOP: instrument, units, price
        - MARKET_IF_TOUCHED: instrument, units, price
        - TAKE_PROFIT: trade_id, price
        - STOP_LOSS: trade_id, price
        - TRAILING_STOP_LOSS: trade_id, distance

        Args:
            order_type: One of MARKET, LIMIT, STOP, MARKET_IF_TOUCHED,
                TAKE_PROFIT, STOP_LOSS, TRAILING_STOP_LOSS.
            instrument: Instrument name (required for entry orders).
            units: Number of units (positive=buy, negative=sell).
            price: Trigger/limit price (required for LIMIT, STOP, MIT, TP, SL).
            trade_id: Trade ID (required for TP, SL, TSL dependent orders).
            distance: Trailing stop distance (required for TRAILING_STOP_LOSS).
            stop_loss: Stop loss price to attach via stopLossOnFill.
            take_profit: Take profit price to attach via takeProfitOnFill.
            trailing_stop_distance: Trailing stop distance for on-fill attachment.
            time_in_force: Time in force (FOK, GTC, GTD, GFD). Default GTC.
            client_extensions: Optional dict with id, tag, comment.

        Returns dict with order creation and fill transactions.
        """
        ot = order_type.upper()
        client = get_client()

        entry_types = {"MARKET", "LIMIT", "STOP", "MARKET_IF_TOUCHED"}
        dependent_types = {"TAKE_PROFIT", "STOP_LOSS", "TRAILING_STOP_LOSS"}

        if ot not in entry_types and ot not in dependent_types:
            return {
                "error": f"Unknown order_type: {order_type}",
                "valid_types": sorted(entry_types | dependent_types),
            }

        # Validate required params for entry orders
        if ot in entry_types:
            if instrument is None or units is None:
                return {
                    "error": f"{ot} order requires instrument and units",
                    "required": ["instrument", "units"],
                    "provided": {
                        "instrument": instrument,
                        "units": units,
                    },
                }
            if ot != "MARKET" and price is None:
                return {
                    "error": f"{ot} order requires price",
                    "required": ["price"],
                    "provided": {"price": price},
                }

        # Validate required params for dependent orders
        if ot in dependent_types:
            if trade_id is None:
                return {
                    "error": f"{ot} order requires trade_id",
                    "required": ["trade_id"],
                    "provided": {"trade_id": trade_id},
                }
            if ot == "TRAILING_STOP_LOSS" and distance is None:
                return {
                    "error": "TRAILING_STOP_LOSS order requires distance",
                    "required": ["distance"],
                    "provided": {"distance": distance},
                }
            if ot in ("TAKE_PROFIT", "STOP_LOSS") and price is None:
                return {
                    "error": f"{ot} order requires price",
                    "required": ["price"],
                    "provided": {"price": price},
                }

        try:
            if ot == "MARKET":
                return client.place_market_order(
                    instrument=instrument,
                    units=units,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    trailing_stop_distance=trailing_stop_distance,
                    client_extensions=client_extensions,
                )
            elif ot == "LIMIT":
                return client.place_limit_order(
                    instrument=instrument,
                    units=units,
                    price=price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    trailing_stop_distance=trailing_stop_distance,
                    client_extensions=client_extensions,
                    time_in_force=time_in_force,
                )
            elif ot == "STOP":
                return client.place_stop_order(
                    instrument=instrument,
                    units=units,
                    price=price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    trailing_stop_distance=trailing_stop_distance,
                    client_extensions=client_extensions,
                    time_in_force=time_in_force,
                )
            elif ot == "MARKET_IF_TOUCHED":
                return client.place_market_if_touched_order(
                    instrument=instrument,
                    units=units,
                    price=price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    trailing_stop_distance=trailing_stop_distance,
                    client_extensions=client_extensions,
                    time_in_force=time_in_force,
                )
            elif ot == "TAKE_PROFIT":
                return client.place_take_profit_order(
                    trade_id=trade_id,
                    price=price,
                    time_in_force=time_in_force,
                    client_extensions=client_extensions,
                )
            elif ot == "STOP_LOSS":
                return client.place_stop_loss_order(
                    trade_id=trade_id,
                    price=price,
                    time_in_force=time_in_force,
                    client_extensions=client_extensions,
                )
            elif ot == "TRAILING_STOP_LOSS":
                return client.place_trailing_stop_loss_order(
                    trade_id=trade_id,
                    distance=distance,
                    time_in_force=time_in_force,
                    client_extensions=client_extensions,
                )
        except OandaAPIError as e:
            return {"error": str(e)}

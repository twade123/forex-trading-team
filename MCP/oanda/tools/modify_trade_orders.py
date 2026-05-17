"""MCP tool: modify_trade_orders -- set/modify TP, SL, TSL on a trade."""

from typing import Optional

from Source.oanda_client import OandaAPIError


def register(mcp, get_client):
    """Register the modify_trade_orders tool."""

    @mcp.tool()
    def modify_trade_orders(
        trade_id: str,
        take_profit_price: Optional[str] = None,
        stop_loss_price: Optional[str] = None,
        trailing_stop_distance: Optional[str] = None,
    ) -> dict:
        """Set or modify take-profit, stop-loss, and/or trailing stop on a trade.

        Pass a price/distance to set or update the order. Pass "0" as
        the price/distance to cancel the existing dependent order.
        Omit a parameter to leave it unchanged.

        Args:
            trade_id: The trade identifier.
            take_profit_price: Take profit price (e.g. "1.1100").
                Pass "0" to cancel existing TP.
            stop_loss_price: Stop loss price (e.g. "1.0900").
                Pass "0" to cancel existing SL.
            trailing_stop_distance: Trailing stop distance in price
                units (e.g. "0.0050"). Pass "0" to cancel existing TSL.

        Returns dict with trade modification transactions.
        """
        tp = None
        if take_profit_price is not None:
            tp = {"price": take_profit_price}

        sl = None
        if stop_loss_price is not None:
            sl = {"price": stop_loss_price, "timeInForce": "GTC"}

        tsl = None
        if trailing_stop_distance is not None:
            tsl = {"distance": trailing_stop_distance}

        try:
            return get_client().set_trade_orders(
                trade_id=trade_id,
                take_profit=tp,
                stop_loss=sl,
                trailing_stop_loss=tsl,
            )
        except OandaAPIError as e:
            return {"error": str(e)}

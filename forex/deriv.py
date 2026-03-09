
import MetaTrader5 as mt5



# Connect to MT5

def mt5_connect():
    if not mt5.initialize():
        print("initialize() failed, error code =", mt5.last_error())
        quit()



def buy(symbol, volume):
    # Place a buy order
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY,
        "price": mt5.symbol_info_tick(symbol).ask,
        "deviation": 10,
        "magic": 234000,
        "comment": "python script open",
    }
    result = mt5.order_send(request)
    print("Buy order result:", result)

def sell(symbol, volume):
    # Place a sell order
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_SELL,
        "price": mt5.symbol_info_tick(symbol).bid,
        "deviation": 10,
        "magic": 234000,
        "comment": "python script open",
    }
    result = mt5.order_send(request)
    print("Sell order result:", result)


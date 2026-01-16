

from dotenv import load_dotenv
import os
import logging
from binance.client import Client


class ArbitrageAgent:
    """Agent that can place buy/sell orders on Binance.

    Features:
    - Uses `BINANCE_API_KEY` and `BINANCE_API_SECRET` from environment by default.
    - `dry_run=True` prevents live orders and only logs actions (safe default).
    - Supports simple `MARKET` and `LIMIT` orders.
    """

    def __init__(self, api_key: str | None = None, api_secret: str | None = None, *, dry_run: bool = True, testnet: bool = True):
        load_dotenv()
        self.logger = logging.getLogger(__name__)
        logging.basicConfig(level=logging.INFO)

        api_key = api_key or os.getenv('BINANCE_API_KEY')
        api_secret = api_secret or os.getenv('BINANCE_API_SECRET')

        self.dry_run = dry_run
        self.testnet = testnet

        if api_key and api_secret:
            self.client = Client(api_key, api_secret)
            if testnet:
                # Point to Binance testnet REST endpoint when requested.
                # This avoids accidental live orders while allowing real API calls on testnet.
                try:
                    self.client.API_URL = 'https://testnet.binance.vision/api'
                except Exception:
                    pass
        else:
            self.client = None

    def _ensure_client(self):
        if not self.client:
            raise RuntimeError('Binance client not configured. Set BINANCE_API_KEY and BINANCE_API_SECRET or pass keys to constructor.')

    def buy(self, symbol: str, quantity: float, price: float | None = None, order_type: str = 'MARKET') -> dict:
        """Place a buy order.

        - `symbol`: trading pair e.g. 'BTCUSDT' or 'ETHUSDT'
        - `quantity`: amount to buy (in base asset units)
        - `price`: required for LIMIT orders
        - `order_type`: 'MARKET' or 'LIMIT'
        Returns the exchange response (or a dry-run dict).
        """
        self.logger.info('Buy requested: %s qty=%s price=%s type=%s dry_run=%s', symbol, quantity, price, order_type, self.dry_run)
        if self.dry_run:
            return {'dry_run': True, 'action': 'BUY', 'symbol': symbol, 'quantity': quantity, 'price': price, 'order_type': order_type}

        self._ensure_client()

        try:
            if order_type.upper() == 'MARKET':
                resp = self.client.order_market_buy(symbol=symbol, quantity=quantity)
            elif order_type.upper() == 'LIMIT':
                if price is None:
                    raise ValueError('Limit orders require a `price`')
                resp = self.client.create_order(
                    symbol=symbol,
                    side='BUY',
                    type='LIMIT',
                    timeInForce='GTC',
                    quantity=quantity,
                    price=str(price),
                )
            else:
                raise ValueError(f'Unsupported order_type: {order_type}')
            self.logger.info('Buy order response: %s', resp)
            return resp
        except Exception as exc:
            self.logger.exception('Error placing buy order')
            return {'error': str(exc)}

    def sell(self, symbol: str, quantity: float, price: float | None = None, order_type: str = 'MARKET') -> dict:
        """Place a sell order.

        Same args as `buy` but for SELL side.
        """
        self.logger.info('Sell requested: %s qty=%s price=%s type=%s dry_run=%s', symbol, quantity, price, order_type, self.dry_run)
        if self.dry_run:
            return {'dry_run': True, 'action': 'SELL', 'symbol': symbol, 'quantity': quantity, 'price': price, 'order_type': order_type}

        self._ensure_client()

        try:
            if order_type.upper() == 'MARKET':
                resp = self.client.order_market_sell(symbol=symbol, quantity=quantity)
            elif order_type.upper() == 'LIMIT':
                if price is None:
                    raise ValueError('Limit orders require a `price`')
                resp = self.client.create_order(
                    symbol=symbol,
                    side='SELL',
                    type='LIMIT',
                    timeInForce='GTC',
                    quantity=quantity,
                    price=str(price),
                )
            else:
                raise ValueError(f'Unsupported order_type: {order_type}')
            self.logger.info('Sell order response: %s', resp)
            return resp
        except Exception as exc:
            self.logger.exception('Error placing sell order')
            return {'error': str(exc)}





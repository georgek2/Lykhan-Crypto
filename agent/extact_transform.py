import os
import time
from collections import deque
import logging
from dotenv import load_dotenv
from binance.client import Client
from binance import ThreadedWebsocketManager

'''
Docstring for agent.extact_transform
Takes care of extracting and transforming data for the agent module.
'''

load_dotenv()


binance_api_key = os.getenv('BINANCE_API_KEY')
binance_api_secret = os.getenv('BINANCE_API_SECRET')
client = Client(binance_api_key, binance_api_secret)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


# Extract from various sources
class BinanceExtractor:

    '''
    Docstring for BinanceExtractor
    Extracts data from external APIs.

    + Binance API
    '''

    def __init__(self):
        self.twm = None
        # latest holds the most recent tick received from websocket
        self.latest = {}
        # history stores recent ticks as (timestamp, bid, ask)
        self.history = deque()
        # keep history length in seconds (trim older entries)
        self.history_max_seconds = 30

    def binance_websocket(self, msg):
        """Websocket callback for symbol ticker messages.

        Expected fields (when successful): 'c' (last price), 'b' (best bid), 'a' (best ask).
        This method prints bid/ask/last to the terminal.
        """
        try:
            if not isinstance(msg, dict):
                logger.debug('Received non-dict message: %r', msg)
                return

            # Detect error messages from the socket
            if msg.get('e') == 'error' or msg.get('error'):
                logger.error('Binance websocket error: %r', msg)
                return

            last = msg.get('c')
            bid = msg.get('b')
            ask = msg.get('a')

            # store latest values so other parts of the program can read them
            self.latest = { 'c': last, 'b': bid, 'a': ask }

            # push to history with timestamp and trim old entries
            now = time.time()
            try:
                self.history.append((now, bid, ask))
            except Exception:
                logger.exception('Error appending to history')

            # pop left while oldest entry is older than history_max_seconds
            try:
                while self.history and (now - self.history[0][0]) > self.history_max_seconds:
                    self.history.popleft()
            except Exception:
                logger.exception('Error trimming history')

            # log a concise line for visibility
            logger.info('BTCUSDT  last=%s  bid=%s  ask=%s', last, bid, ask)
        except Exception:
            logger.exception('Error processing websocket message')

    def subscribe_stream(self):
        # if not binance_api_key or not binance_api_secret:
        #     logger.error('BINANCE_API_KEY and BINANCE_API_SECRET must be set in environment')
        #     return

        # ThreadedWebsocketManager can run without API keys for public market streams.
        self.twm = ThreadedWebsocketManager(api_key=binance_api_key or None, api_secret=binance_api_secret or None)
        self.twm.start()
        # start_symbol_ticker_socket delivers frequent updates for the given symbol
        self.twm.start_symbol_ticker_socket(callback=self.binance_websocket, symbol='BROCCOLI714USDT')
        time.sleep(3)  # give a moment for the first tick to arrive
        return self

    def stop(self):
        if self.twm:
            try:
                self.twm.stop()
            except Exception:
                logger.exception('Error stopping websocket manager')
            self.twm = None

class ArbitrageExtractor:

    '''
    Docstring for ArbitrageExtractor
    Extracts arbitrage opportunities from multiple exchanges.
    '''

    def opportunity_identifier(self, lag_seconds=3.0, poll_interval=1.0):
        """Start the Binance extractor and poll latest ticks to compute simple arbitrage.

        This uses a small lag window: it compares the current best-bid to the
        best-ask from approximately `lag_seconds` ago. That makes short-lived
        fluctuations more likely to produce a detectable "opportunity".
        """
        extractor = BinanceExtractor().subscribe_stream()
        try:
            while True:
                now = time.time()

                # current tick
                latest = extractor.latest
                cur_bid = latest.get('b')
                cur_ask = latest.get('a')

                # find an older tick roughly `lag_seconds` ago (first match from left)
                target_time = now - lag_seconds
                old_ask = None
                for ts, b, a in extractor.history:
                    if ts <= target_time:
                        old_ask = a
                    else:
                        break

                if cur_bid and old_ask:
                    try:
                        bid_f = float(cur_bid)
                        old_ask_f = float(old_ask)
                        profit = bid_f - old_ask_f
                        if profit > 0:
                            print(f"Lag arb! Buy at {old_ask}, sell now at {cur_bid}, profit: {profit:.8f}")
                        else:
                            print(f"No lag arb: spread={profit:.8f}")
                    except ValueError:
                        logger.debug('Non-numeric values in lag calculation: %r %r', cur_bid, old_ask)
                else:
                    print('Waiting for enough history or current market data...')

                time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info('Interrupted, stopping scanner')
            extractor.stop()
            
class Transformer:

    '''
    Docstring for Transformer
    Transforms extracted data into required formats.
    '''

    def normalize(self, data):
        pass

    def aggregate(self, data):
        pass


def main():
    binance = ArbitrageExtractor()
    binance.opportunity_identifier()

    try:
        # Keep the main thread alive to continue receiving websocket messages
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        logger.info('Interrupted, stopping websocket manager')
        if binance.twm:
            binance.twm.stop()


if __name__ == '__main__':
    main()

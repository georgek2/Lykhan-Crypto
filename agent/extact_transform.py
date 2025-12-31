import os
import time
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

    def binancewebsocket(self, msg):
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

            # Print a concise line showing the changing prices
            if last is not None or bid is not None or ask is not None:
                print(f"BTCUSDT  last={last}  bid={bid}  ask={ask}")
        except Exception:
            logger.exception('Error processing websocket message')

    def subscribestream(self):
        if not binance_api_key or not binance_api_secret:
            logger.error('BINANCE_API_KEY and BINANCE_API_SECRET must be set in environment')
            return

        self.twm = ThreadedWebsocketManager(api_key=binance_api_key, api_secret=binance_api_secret)
        self.twm.start()
        # start_symbol_ticker_socket delivers frequent updates for the given symbol
        self.twm.start_symbol_ticker_socket(callback=self.binancewebsocket, symbol='BTCUSDT')



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
    binance = BinanceExtractor()
    binance.subscribestream()

    try:
        # Keep the main thread alive to continue receiving websocket messages
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info('Interrupted, stopping websocket manager')
        if binance.twm:
            binance.twm.stop()


if __name__ == '__main__':
    main()

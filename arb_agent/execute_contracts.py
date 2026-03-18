
import os 
from dotenv import load_dotenv
import logging 
from binance.client import Client 



class ArbitrageAgent:

    '''
        Agent responsible for making buy and sell orders based on 
        identified opportunities and potential profits
    '''

    def __init__(self):
        load_dotenv()

        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_API_SECRET')
        self.client = Client(api_key, api_secret)

        # Setting up logging for system processes
        self.logger = logging.getLogger(__name__)
        logging.basicConfig(level=logging.INFO)

    def buy(self, symbol: str, quantity: float, price: float):
        '''
        Buys contracts and waits for sell after price goes up
        '''
        self.logger.info(
            f'Buy requested: {symbol} - {quantity} ...Price: {price}' 
        )

    def sell(self, symbol: str, quantity: float, price: float):
        ''' 
        Sells pair bought earlier for a reasonable profit

        Or takes shot trades if price is going down
        '''
        self.logger.info(
            f'Sell requested: {symbol} - {quantity} ...Price: {price}' 
        )


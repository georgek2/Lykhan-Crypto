import os 
from  dotenv import load_dotenv

'''
Docstring for agent.extact_transform
Takes care of extracting and transforming data for the agent module.
'''

load_dotenv()


# Extract from various sources 
class APIExtractor: 

    '''
    Docstring for APIExtractor
    Extracts data from external APIs.

    + Binance API
    + CoinGecko API
    + Other financial data APIs
    '''

    def binance(self):

        binance_api_key = os.getenv('BINANCE_API_KEY')
        binance_api_secret = os.getenv('BINANCE_API_SECRET')
        pass

    def coingecko(self):
        coingecko_api_key = os.getenv('COINGECKO_API_KEY')
        coingecko_api_secret = os.getenv('COINGECKO_API_SECRET')
        pass



class Transformer: 

    '''
    Docstring for Transformer
    Transforms extracted data into required formats.
    '''

    def normalize(self, data):
        pass

    def aggregate(self, data):
        pass




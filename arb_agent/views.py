from django.shortcuts import render
import os 
from  dotenv import load_dotenv
from binance.client import Client
'''
Docstring for agent.extact_transform
Takes care of extracting and transforming data for the agent module.
'''

load_dotenv()


binance_api_key = os.getenv('BINANCE_API_KEY')
binance_api_secret = os.getenv('BINANCE_API_SECRET')
client = Client(os.getenv('BINANCE_API_KEY'), os.getenv('BINANCE_API_SECRET'))


print(client.get_asset_balance(asset='PEPE'))
# Create your views here.
pepe = client.get_symbol_ticker(symbol='PEPEUSDT')
print(pepe)

def home(request):

    context = {
        'dummy_value': 2026,
    }

    return render(request, 'agent/agent.html', context=context)





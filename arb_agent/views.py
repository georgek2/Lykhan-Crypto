from django.shortcuts import render
import os
from dotenv import load_dotenv
from binance.client import Client

load_dotenv()

def home(request):
    # The Binance client and API calls now live INSIDE the view function.
    # They only execute when someone actually requests this page,
    # not at Django startup time.
    binance_api_key = os.getenv('BINANCE_API_KEY')
    binance_api_secret = os.getenv('BINANCE_API_SECRET')
    
    client = Client(binance_api_key, binance_api_secret)
    pepe_balance = client.get_asset_balance(asset='PEPE')
    pepe = client.get_symbol_ticker(symbol='PEPEUSDT')
    
    print(pepe_balance)
    print(pepe)

    context = {
        'dummy_value': 2026,
    }

    return render(request, 'agent/agent.html', context=context)
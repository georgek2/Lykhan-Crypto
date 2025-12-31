# LYKHAN
My AI powered autonomous crypto arbitrage bot running on AWS. Uses a django backend. Performs on-chain combined with cross-exchange arbitrage transactions to maximize profits.

## Key Components
+ Data Collection: Fetch prices from multiple exchanges.
+ Decision Engine: Calculate arbitrage opportunities (considering fees, transfer times, etc.).
+ Execution Engine: Automate the buying, transferring, and selling of crypto.
+ Risk Management: Monitor balances, set stop-losses, etc.
+ Backend: Django for managing state, logging, and providing a dashboard.


## Workflow
> ETL
Collect crypto data(Price + Changes, MarketCap, Volume) and news from;
+ Binance API
+ CoinGecko
+ CoinMarketCap
+ Other: News sites, magazines, official crypto sites, social media

Transform and organize the data using python, store on AWS S3 for analysis and inference.

Attach ML + LLMs on the data to derive trading signals and calculate potentail profit:

## Tools and Skills:
+ Django (Python)
+ LLMs (Claude, DeepSeek, Gemini) for natural language instructions (possibly for interpreting exchange rules or unexpected situations).
+ Playwright for browser automation.
+ AWS: EC2, Lambda, Step Functions, Elastic Beanstalk, VPC.

## Design Principles:
SOLID, ACID (for database transactions).

# Architecture Overview
1. Data Collection Module
Purpose: Continuously collect real-time prices from multiple exchanges.

Implementation:

Use exchange APIs (REST/WebSocket) for price data.

If API is not available, use Playwright to scrape the data (fallback).

Normalize data (pair names, prices, fees) into a common format.

2. Arbitrage Calculation Engine
Purpose: Identify arbitrage opportunities.

Implementation:

Calculate potential profit for each pair across exchanges (considering trading fees, withdrawal fees, transfer times, and gas fees).

Filter opportunities by minimum profit threshold.

Rank opportunities by expected return or risk-adjusted return.

3. Execution Module
Purpose: Execute the arbitrage trades.

Implementation:

Use exchange APIs for trading (preferred) or Playwright for browser automation if API is not available.

Handle the entire flow: buy, transfer, sell.

Monitor execution and handle errors.

4. Risk Management Module
Purpose: Monitor overall risk, set position limits, stop-losses, etc.

Implementation:

Track balances across exchanges.

Set limits on maximum exposure per exchange, per currency, or per trade.

5. Backend (Django)
Purpose: Manage state, log activities, and provide a dashboard.

Implementation:

Use Django ORM for database models (Trade, Exchange, Currency, Balance, etc.).

Use Django REST Framework for APIs (if needed for frontend or external monitoring).

6. LLM Integration
Purpose: Handle unexpected situations (like captchas, exchange rule changes) by interpreting natural language instructions and making decisions.

Implementation:

Use LLM APIs (Claude, DeepSeek, Gemini) to process instructions or to make decisions in uncertain scenarios.

7. Cloud Infrastructure (AWS)
Purpose: Host the agent and ensure it runs 24/7.

Implementation:

Use EC2 for the main Django application and Playwright (requires a browser environment).

Use Lambda for serverless functions (maybe for data collection or periodic tasks).

Use Step Functions to orchestrate complex workflows (like the entire arbitrage process for an opportunity).

Use Elastic Beanstalk for easy deployment and scaling of Django.

Use VPC for networking security.

Given the above, let's design the classes and workflows.


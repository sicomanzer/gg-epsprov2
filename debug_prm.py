import yfinance as yf
import os

# Set cache path
yf.set_tz_cache_location("yfinance_cache")

ticker = "AAPL"
stock = yf.Ticker(ticker)
info = stock.info

print(f"Ticker: {ticker}")
print(f"dividendYield: {info.get('dividendYield')}")

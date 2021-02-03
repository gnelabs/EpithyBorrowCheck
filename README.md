# EpithyBorrowCheck

Python script to perform ad-hoc lookups of stock options data in order to find loan fee arbitrage opportunities.

Get free money from short sellers. To be used by experienced traders only.

## Assumptions

* Ad-hoc lookups only.
* Use this in conjunction with a screener.
* This uses the spot loan fee, so apply your own technical analysis to historical fee rates.
* Calculations are pre-tax.

## Prerequisites

### Tradier sandbox API key

Create a Tradier sandbox developer API key. This is free and will allow you to get realtime options data required for this script.
https://developer.tradier.com/user/sign_up

Paste the key into tradier_bearer.txt file included with this script for it to work. This script will read that file.
Should be in the JWT format example: Bearer asdf87aysdf87asydf87asydf87

### Libraries

Install these with pip (python3):

tqdm
columnar
click

## Usage

python .\borrow_check.py

Input the stock symbol, fee rate, and utilization rate and it does the rest.

## Sharp edges around loan fee arbitrage

* You need a broker that loans out your shares and pays you a split. This broker needs to allow writing options against this position for hedging. Only a few brokers do this, so understand the risks and limitations associated with short selling. This script assumes you'll be using IBKR.
* Short dated high delta ITM call options tend to be frequently early exercised for stocks with high volatility and high loan fees. Be careful not to lose money on the bid and calculate your intrinsic value with prudence.
* If done properly you should have little or no directional risk, however stocks that go parabolic can cause margin calls.
* In most cases, you'll be dealing with wide spreads. Use L2 as needed when putting on a position and keep liquidity in mind.
* If you're near expiration and the stock is close to your collar strike, it's better to manually close the position then to assume pin risk. The calculations conservatively assume you will be doing this.
* Percentage of shares loaned won't always be at the utilization, but usually near it. I don't have the data to calculate the median for this so assume some slippage.
* Shares typically get loaned out at the next trading day locate auction in the morning.
* These stocks can often be fast moving, so you should be comfortable with complex order types.

## Example

![Example usage](https://github.com/gnelabs/EpithyBorrowCheck/blob/main/example/screenshot.png?raw=true)

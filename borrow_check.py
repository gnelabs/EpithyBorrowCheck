from decimal import Decimal
from datetime import datetime, timedelta
from math import ceil
from os import getcwd
from pathlib import Path
from collections import OrderedDict, defaultdict
import requests
#For progress bar in CLI. pip install tqdm
import tqdm
#Pretty columns for CLI display. pip install columnar
from columnar import columnar
#CLI colors. pip install click
from click import style

# constants
OPTION_CONTRACT_COST = 1 #Assuming a one-lot contract, $1 minimum. Conservative estimate.
CONTRACT_ACTIONS_PER_COLLAR = 4 #Number of contract actions required to manage a collar without pin risk.
LOAN_ADJUSTED_RATE = Decimal(253 / 365) #Fee rate is calculated in annual APR, but is only paid out on trading days.
IBKR_FEE_SPLIT = Decimal(0.5) #Interactive Brokers pays you 50% of the rate.
OPTION_CONTRACT_SIZE = 100 #Nobody trades fractional lots anymore, do they?

def input_section() -> dict:
    """
    Inputs, defaults to str unless converted.
    """
    input_data = {}
    input_data['symbol'] = input('Stock symbol:> ').upper().replace(' ','').replace('$', '')
    input_data['util'] = Decimal(input('Utilization rate:> ')) / 100 #Inputted as percentage.
    input_data['borrow_rate'] = Decimal(input('Current borrow rate percentage:> ')) / 100 #Inputted as percentage.
    return input_data


class MissingAPIKeyException(Exception):
    pass


def tradier_key() -> str:
    """
    Verifies and grabs Tradier key used to query the API.
    """
    my_file = Path(getcwd() + "/tradier_bearer.txt")
    windows_is_stupid_file = Path(getcwd() + "/tradier_bearer.txt.txt") #Ugh, fucking windows.
    
    if not my_file.is_file():
        raise MissingAPIKeyException('Problem with Tradier API key. Unable to find file holding the key.')
    elif windows_is_stupid_file.is_file():
        raise MissingAPIKeyException('Problem with Tradier API key. Windows is stupid please double check the file name.')
    
    with open(my_file, 'r') as apikeyfile:
        api_key = apikeyfile.read()
        api_key.rstrip() #Remove newlines, carriage returns just in case.
        if 'Bearer' in api_key:
            return api_key
        else:
            raise MissingAPIKeyException('Problem with Tradier API key. Did not find correct format in key file.')

class Queries(object):
    def __init__(self, symbol: str, api_key: str):
        #to-do: Read the bearer token from a file or something.
        self.headers = {"Accept": "application/json", "Authorization": api_key}
        self.api = 'https://sandbox.tradier.com{0}'
        self.ratelimit_available = 120 #I think they say 120 calls/min?
        self.symbol = symbol
    
    def quotes(self) -> dict:
        """
        Using the inputted quote, grab realtime prices. Used to calculate spreads.
        """
        url = self.api.format('/v1/markets/quotes')
        params = {'symbols': self.symbol}
        try:
            r = requests.get(url, headers=self.headers, params=params)
            self.ratelimit_available = int(r.headers['X-Ratelimit-Available'])
            return r.json()['quotes']['quote']
        except Exception as e:
            raise Exception('Problem querying stock quotes. Status code: {0} Error: {1}'.format(r.status_code, e))
    
    def expirations(self) -> list:
        """
        Need to gather available options expirations before querying the chains.
        """
        url = self.api.format('/v1/markets/options/expirations')
        params = {'symbol': self.symbol, 'includeAllRoots': 'true', 'strikes': 'false'}
        try:
            r = requests.get(url, headers=self.headers, params=params)
            self.ratelimit_available = int(r.headers['X-Ratelimit-Available'])
            return r.json()['expirations']['date']
        except Exception as e:
            raise Exception('Problem querying expirations for {0}. Status code: {1} Error: {2}'.format(self.symbol , r.status_code, e))
    
    def options_chain(self, expiration_date: str) -> list:
        """
        With the option expiration grab the options chain data.
        """
        url = self.api.format('/v1/markets/options/chains')
        params = {'symbol': self.symbol, 'expiration': expiration_date, 'greeks': 'true'}
        try:
            r = requests.get(url, headers=self.headers, params=params)
            self.ratelimit_available = int(r.headers['X-Ratelimit-Available'])
            return r.json()['options']['option']
        except Exception as e:
            raise Exception('Problem querying options chain for {0}. Status code: {1} Error: {2}'.format(self.symbol , r.status_code, e))
    
    def gather_data(self) -> dict:
        """
        Gather and go through the options expirations to collect the chain data.
        Compile it into a dict to be used for calculation.
        """
        print('Grabbing current stock price and options expirations.')
        compiled_data_for_symbol = {}
        
        expirations = self.expirations()
        compiled_data_for_symbol['stock_quote'] = self.quotes()
        compiled_data_for_symbol['options_data'] = []
        
        for expiration in tqdm.tqdm(expirations):
            options_data = self.options_chain(expiration)
            compiled_data_for_symbol['options_data'].append({expiration: options_data})
        
        return compiled_data_for_symbol


class Calculations(object):
    def __init__(self):
        #Dictionary to store details and data for symmetric collar information.
        self.overall_data_symmetric = {}
        
        #Dictionary to store details and data for asymmetric collar information.
        self.overall_data_asymmetric = {}
    
    def calculate_symmetric_collar(self, options_data: dict, util: Decimal, borrow_rate: Decimal) -> tuple:
        """
        Symmetric collar calculations. Assumes you'll be selling an ITM call and buying an OTM put at the same strike.
        """
        stock_price = Decimal(options_data['stock_quote']['ask']) #Using the stock ask for quick fill assumption.
        symbol = options_data['stock_quote']['symbol']
        
        #Single occurance calculations.
        daily_fee_payout_amount_per_share_without_utilization = ((( stock_price * ( borrow_rate * IBKR_FEE_SPLIT )) / 365 ) * LOAN_ADJUSTED_RATE )
        daily_payout_per_options_contract_before_fees = (( daily_fee_payout_amount_per_share_without_utilization * util ) * OPTION_CONTRACT_SIZE )
        options_fees_paid = OPTION_CONTRACT_COST * CONTRACT_ACTIONS_PER_COLLAR
        buying_power_required = stock_price * OPTION_CONTRACT_SIZE
        
        #Dictionary sorted by lowest risk. Uses breakeven days.
        trades_by_risk = {}
        
        #Dictionary sorted by max profit. Uses anualized performance.
        trades_by_profit = {}
        
        for expiration_list_item in options_data['options_data']:
            for option_chain_for_expiration in expiration_list_item.items():
                expiration_date = option_chain_for_expiration[0]
                option_expiration_days_remaining = datetime.strptime(expiration_date, "%Y-%m-%d") - datetime.now()
                
                #Both puts and calls are needed to calulate profit, but this is listed individually as a list item.
                #Do a first pass to combine the two.
                options_bid_ask_prices = defaultdict(dict)
                for strike_price_data_first_pass in option_chain_for_expiration[1]:
                    if strike_price_data_first_pass['option_type'] == 'call':
                        options_bid_ask_prices[Decimal(strike_price_data_first_pass['strike'])]['call_bid'] = Decimal(strike_price_data_first_pass['bid'])
                    elif strike_price_data_first_pass['option_type'] == 'put':
                        options_bid_ask_prices[Decimal(strike_price_data_first_pass['strike'])]['put_ask'] = Decimal(strike_price_data_first_pass['ask'])
                
                for strike_price_data_second_pass in option_chain_for_expiration[1]:
                    #Since a first pass was already done, skip running calculations for puts.
                    #It's just the same data over again.
                    if strike_price_data_second_pass['option_type'] == 'put':
                        continue
                    else:
                        total_payout_before_fees = daily_payout_per_options_contract_before_fees * option_expiration_days_remaining.days
                        option_strike = Decimal(strike_price_data_second_pass['strike'])
                        occ_options_symbol = strike_price_data_second_pass['symbol']
                        
                        #Negative if debit, positive if credit
                        expiration_net = ((( options_bid_ask_prices[option_strike]['call_bid'] - options_bid_ask_prices[option_strike]['put_ask'] ) - ( stock_price - option_strike )) * OPTION_CONTRACT_SIZE )
                        
                        #Convert to positive since it's going to be a fee.
                        if expiration_net > 0:
                            cost_of_trade = 0
                            charge_type = 'credit'
                            cost_of_trade_per_day = cost_of_trade
                        else:
                            cost_of_trade = abs(expiration_net - options_fees_paid)
                            charge_type = 'debit'
                            #Shares get loaned out at the morning auction. If days are zero, this is a losing trade.
                            if option_expiration_days_remaining.days == 0:
                                cost_of_trade_per_day = cost_of_trade
                            else:
                                cost_of_trade_per_day = cost_of_trade / option_expiration_days_remaining.days
                        
                        fee_payout_minus_slippage_and_fees = total_payout_before_fees - cost_of_trade
                        annualized_play_performance = ((( daily_payout_per_options_contract_before_fees - cost_of_trade_per_day ) / buying_power_required ) * 36500 )
                        breakeven_borrow_rate = (((( cost_of_trade_per_day / buying_power_required) * 36500 ) / IBKR_FEE_SPLIT ) / LOAN_ADJUSTED_RATE )
                        days_to_profit = ceil(( cost_of_trade / daily_payout_per_options_contract_before_fees ))
                        
                        #ToS format. Not really needed at the moment, but kept here in case I want to use it later.
                        #trade_description = '${0} Collar ${1} for {2} ({3})'.format(symbol, option_strike, expiration_date, option_expiration_days_remaining.days)
                        
                        trades_by_risk[occ_options_symbol] = days_to_profit
                        trades_by_profit[occ_options_symbol] = fee_payout_minus_slippage_and_fees
                        
                        self.overall_data_symmetric[occ_options_symbol] = {
                            'days_to_profit': days_to_profit,
                            'annualized_play_performance': '{0}%'.format(round(annualized_play_performance, 2)),
                            'breakeven_borrow_rate': '{0}%'.format(round(breakeven_borrow_rate, 2)),
                            'call_moneyness': 'itm' if option_strike < stock_price else 'otm',
                            'estimated_payout': '${0}'.format(round(fee_payout_minus_slippage_and_fees, 2)),
                            'cost_of_trade_per_day': '${0}'.format(round(cost_of_trade_per_day, 2)),
                            'expiration_net': '${0}'.format(round(expiration_net, 2)),
                            'strike': '${0}'.format(round(option_strike, 2)),
                            'expiration_date': expiration_date,
                            'profitable': True if days_to_profit < option_expiration_days_remaining.days else False
                        }
        
        return (trades_by_risk, trades_by_profit)
    
    def calculate_asymmetric_collar(self, options_data: dict, util: Decimal, borrow_rate: Decimal) -> tuple:
        """
        Asymmetric collar calculations. Assumes you'll be selling an OTM call and buying an OTM put at different strikes.
        """
        stock_price = Decimal(options_data['stock_quote']['ask']) #Using the stock ask for quick fill assumption.
        symbol = options_data['stock_quote']['symbol']
        
        #Single occurance calculations.
        daily_fee_payout_amount_per_share_without_utilization = ((( stock_price * ( borrow_rate * IBKR_FEE_SPLIT )) / 365 ) * LOAN_ADJUSTED_RATE )
        daily_payout_per_options_contract_before_fees = (( daily_fee_payout_amount_per_share_without_utilization * util ) * OPTION_CONTRACT_SIZE )
        options_fees_paid = OPTION_CONTRACT_COST * CONTRACT_ACTIONS_PER_COLLAR
        buying_power_required = stock_price * OPTION_CONTRACT_SIZE
        
        #Dictionary sorted by lowest risk. Uses breakeven days.
        trades_by_risk = {}
        
        #Dictionary sorted by max profit. Uses anualized performance.
        trades_by_profit = {}
        
        for expiration_list_item in options_data['options_data']:
            for option_chain_for_expiration in expiration_list_item.items():
                expiration_date = option_chain_for_expiration[0]
                option_expiration_days_remaining = datetime.strptime(expiration_date, "%Y-%m-%d") - datetime.now()
                
                total_payout_before_fees = daily_payout_per_options_contract_before_fees * option_expiration_days_remaining.days
                
                #Both puts and calls are needed to calulate profit, but this is listed individually as a list item.
                #Do a first pass to combine the two.
                options_bid_ask_prices = defaultdict(dict)
                for strike_price_data_first_pass in option_chain_for_expiration[1]:
                    if strike_price_data_first_pass['option_type'] == 'call':
                        options_bid_ask_prices[Decimal(strike_price_data_first_pass['strike'])]['call_bid'] = Decimal(strike_price_data_first_pass['bid'])
                    elif strike_price_data_first_pass['option_type'] == 'put':
                        options_bid_ask_prices[Decimal(strike_price_data_first_pass['strike'])]['put_ask'] = Decimal(strike_price_data_first_pass['ask'])
                
                #Since this is asymmetric, calculate all possible otm calculations.
                #Done as a second pass and creates data to be iterrated on for the third pass doing the final calculations.
                #Stored as a dictionary with combination strikes as the key and the expiration net calculation as the value.
                otm_options_collar_combinations = defaultdict(dict)
                available_strikes = options_bid_ask_prices.keys()
                for call_strike in available_strikes:
                    #ITM calls are covered by symmetric collars.
                    if call_strike < stock_price:
                        continue
                    else:
                        for put_strike in available_strikes:
                            if put_strike > stock_price:
                                continue
                            else:
                                combination_strike = '{0}c/{1}p'.format(call_strike, put_strike)
                                #Negative if debit, positive if credit
                                expiration_net = ((( options_bid_ask_prices[call_strike]['call_bid'] - options_bid_ask_prices[put_strike]['put_ask'] ) - ( stock_price - put_strike )) * OPTION_CONTRACT_SIZE )
                            
                                otm_options_collar_combinations[combination_strike] = expiration_net
                
                #Third pass, run the remaining calculations based off of predetermined combinations.
                for collar_combination in otm_options_collar_combinations.items():
                    if collar_combination[1] > 0:
                        cost_of_trade = 0
                        charge_type = 'credit'
                        cost_of_trade_per_day = cost_of_trade
                    else:
                        cost_of_trade = abs(collar_combination[1] - options_fees_paid)
                        charge_type = 'debit'
                        #Shares get loaned out at the morning auction. If days are zero, this is a losing trade.
                        if option_expiration_days_remaining.days == 0:
                            cost_of_trade_per_day = cost_of_trade
                        else:
                            cost_of_trade_per_day = cost_of_trade / option_expiration_days_remaining.days
                    
                    fee_payout_minus_slippage_and_fees = total_payout_before_fees - cost_of_trade
                    annualized_play_performance = ((( daily_payout_per_options_contract_before_fees - cost_of_trade_per_day ) / buying_power_required ) * 36500 )
                    breakeven_borrow_rate = (((( cost_of_trade_per_day / buying_power_required) * 36500 ) / IBKR_FEE_SPLIT ) / LOAN_ADJUSTED_RATE )
                    days_to_profit = ceil(( cost_of_trade / daily_payout_per_options_contract_before_fees ))
                    
                    trades_by_risk[collar_combination[0]] = days_to_profit
                    trades_by_profit[collar_combination[0]] = fee_payout_minus_slippage_and_fees
                    
                    self.overall_data_asymmetric[collar_combination[0]] = {
                        'days_to_profit': days_to_profit,
                        'annualized_play_performance': '{0}%'.format(round(annualized_play_performance, 2)),
                        'breakeven_borrow_rate': '{0}%'.format(round(breakeven_borrow_rate, 2)),
                        'call_moneyness': 'otm',
                        'estimated_payout': '${0}'.format(round(fee_payout_minus_slippage_and_fees, 2)),
                        'cost_of_trade_per_day': '${0}'.format(round(cost_of_trade_per_day, 2)),
                        'expiration_net': '${0}'.format(round(collar_combination[1], 2)),
                        'strike': '${0}'.format(collar_combination[0]),
                        'expiration_date': expiration_date,
                        'profitable': True if days_to_profit < option_expiration_days_remaining.days else False
                    }
        
        return (trades_by_risk, trades_by_profit)


if __name__ == '__main__':
    tradier_sandbox_api_key = tradier_key()
    input = input_section()
    
    queries_obj = Queries(input['symbol'], tradier_sandbox_api_key)
    options_data = queries_obj.gather_data()
    
    #Debug text to help me track remaining API calls.
    print('Debugging: thottling, api calls remaining: {0}'.format(queries_obj.ratelimit_available))
    
    calc_obj = Calculations()
    
    #Output style patterns.
    patterns = [
        ('True', lambda text: style(text, fg='green')),
        ('itm', lambda text: style(text, fg='yellow')),
        ('otm', lambda text: style(text, fg='cyan')),
    ]
    
    #Symmetric output
    best_plays_output_symmetric = calc_obj.calculate_symmetric_collar(
        options_data = options_data,
        util = input['util'],
        borrow_rate = input['borrow_rate']
    )
    
    risk_ascending_symmetric = OrderedDict(sorted(best_plays_output_symmetric[0].items(), key=lambda t: t[1]))
    performance_ascending_symmetric = OrderedDict(sorted(best_plays_output_symmetric[1].items(), key=lambda t: t[1], reverse=True))
    headers_sym = []
    top_5_risk_sym = []
    top_5_perform_sym = []
    
    risk_count = 0
    for item in risk_ascending_symmetric.items():
        top_5_risk_sym.append(list(calc_obj.overall_data_symmetric[item[0]].values()))
        risk_count += 1
        if risk_count == 5:
            #Keys for column output are all the same, so populate it once.
            headers_sym = list(calc_obj.overall_data_symmetric[item[0]].keys())
            break
    
    profit_count = 0
    for item in performance_ascending_symmetric.items():
        top_5_perform_sym.append(list(calc_obj.overall_data_symmetric[item[0]].values()))
        profit_count += 1
        if profit_count == 5:
            break
    
    print('Top 5 symettric collar trades by risk factor:')
    print(columnar(top_5_risk_sym, headers_sym, no_borders=True, patterns=patterns))
    print('Top 5 most profitable symettric collar trades:')
    print(columnar(top_5_perform_sym, headers_sym, no_borders=True, patterns=patterns))
    
    #Asymmetric output
    best_plays_output_asymmetric = calc_obj.calculate_asymmetric_collar(
        options_data = options_data,
        util = input['util'],
        borrow_rate = input['borrow_rate']
    )
    
    risk_ascending_asymmetric = OrderedDict(sorted(best_plays_output_asymmetric[0].items(), key=lambda t: t[1]))
    performance_ascending_asymmetric = OrderedDict(sorted(best_plays_output_asymmetric[1].items(), key=lambda t: t[1], reverse=True))
    headers_asym = []
    top_5_risk_asym = []
    top_5_perform_asym = []
    
    risk_count = 0
    for item in risk_ascending_asymmetric.items():
        top_5_risk_asym.append(list(calc_obj.overall_data_asymmetric[item[0]].values()))
        risk_count += 1
        if risk_count == 5:
            #Keys for column output are all the same, so populate it once.
            headers_asym = list(calc_obj.overall_data_asymmetric[item[0]].keys())
            break
    
    profit_count = 0
    for item in performance_ascending_asymmetric.items():
        top_5_perform_asym.append(list(calc_obj.overall_data_asymmetric[item[0]].values()))
        profit_count += 1
        if profit_count == 5:
            break
    
    print('Top 5 asymettric collar trades by risk factor:')
    print(columnar(top_5_risk_asym, headers_asym, no_borders=True, patterns=patterns))
    print('Top 5 most profitable asymettric collar trades:')
    print(columnar(top_5_perform_asym, headers_asym, no_borders=True, patterns=patterns))
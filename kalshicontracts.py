import requests
import argparse

def main():
    # Set up command-line argument parsing
    parser = argparse.ArgumentParser(description="Fetch Kalshi market data for a specific contract and date.")
    
    # Define the arguments to accept
    parser.add_argument("contract", type=str, help="The contract ticker (e.g., KXWTI, KXGOLDW)")
    parser.add_argument("target_date", type=str, help="The target date in ticker format (e.g., 26MAY26)")
    
    # Parse the arguments provided in the command line
    args = parser.parse_args()
    
    contract = args.contract
    target_date = args.target_date

    markets_url = f"https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker={contract}&status=open"
    markets_response = requests.get(markets_url)
    markets_response.raise_for_status()
    markets_data = markets_response.json()

    print(f"\nActive markets in {contract} series for {target_date}:")

    for market in markets_data["markets"]:
        ticker = market["ticker"]

        # Extract date portion from ticker
        try:
            date_part = ticker.split("-")[1][:7]  # e.g. '26APR22'
        except IndexError:
            continue

        # Filter by date
        event = market["event_ticker"]

        if target_date not in event:
             continue

        yes_bid = market.get("yes_bid_dollars")
        yes_ask = market.get("yes_ask_dollars")
        no_bid = market.get("no_bid_dollars")
        no_ask = market.get("no_ask_dollars")
        last = market.get("last_price_dollars")

        print(f"- {ticker}: {market['title']}")
        print(f"  Event: {market['event_ticker']}")
        print(f"  YES bid:  ${yes_bid}")
        print(f"  YES ask:  ${yes_ask}")
        print(f"  NO bid:   ${no_bid}")
        print(f"  NO ask:   ${no_ask}")
        print(f"  Last:     ${last}")
        print(f"  Volume:   {market.get('volume_fp')}")
        print()

if __name__ == "__main__":
    main()
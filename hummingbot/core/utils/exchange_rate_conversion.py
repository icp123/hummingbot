import asyncio
import logging
import math
from typing import (
    Dict,
    List,
    Optional
)

from hummingbot.client.config.global_config_map import global_config_map
from hummingbot.data_feed.coin_cap_data_feed import CoinCapDataFeed
from hummingbot.data_feed.coin_gecko_data_feed import CoinGeckoDataFeed
from hummingbot.data_feed.data_feed_base import DataFeedBase
from hummingbot.logger import HummingbotLogger

NaN = float("nan")


class ExchangeRateConversion:
    DEFAULT_DATA_FEED_NAME = "coin_gecko_api"
    erc_logger: Optional[HummingbotLogger] = None
    _erc_shared_instance: "ExchangeRateConversion" = None
    _exchange_rate_config_override: Optional[Dict[str, Dict]] = None
    _data_feeds_override: Optional[List[DataFeedBase]] = None
    _update_interval: float = 5.0
    _data_feed_timeout: float = 30.0
    _data_feeds: List[DataFeedBase] = []
    _exchange_rate_config: Dict[str, Dict] = {"conversion_required": {}, "global_config": {}}
    _exchange_rate: Dict[str, float] = {}
    _all_data_feed_exchange_rate: Dict[str, float] = {}
    _started: bool = False
    _ready_notifier: asyncio.Event = asyncio.Event()
    _show_update_exchange_rates_from_data_feeds_errors: bool = True
    _show_wait_till_ready_errors: bool = True

    @property
    def ready_notifier(self) -> asyncio.Event:
        return self._ready_notifier

    @classmethod
    def get_instance(cls) -> "ExchangeRateConversion":
        if cls._erc_shared_instance is None:
            cls._erc_shared_instance = ExchangeRateConversion()
        elif not cls._exchange_rate_config["global_config"]:
            # init config in case the exchange rate instance is initiated before global config map
            cls._erc_shared_instance.init_config()
        return cls._erc_shared_instance

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls.erc_logger is None:
            cls.erc_logger = logging.getLogger(__name__)
        return cls.erc_logger

    @classmethod
    def set_global_exchange_rate_config(cls, config: Dict[str, Dict]):
        if cls._exchange_rate_config_override is None:
            cls._exchange_rate_config_override = config
        else:
            cls._exchange_rate_config_override.clear()
            cls._exchange_rate_config_override.update(config)
        cls.init_config()

    @classmethod
    def set_data_feeds(cls, data_feeds: List[DataFeedBase]):
        if cls._data_feeds_override is None:
            cls._data_feeds_override = data_feeds
        else:
            cls._data_feeds_override.clear()
            cls._data_feeds_override.extend(data_feeds)
        cls.init_config()

    @classmethod
    def set_update_interval(cls, update_interval: float):
        cls._update_interval = update_interval

    @classmethod
    def init_config(cls):
        try:
            if cls._data_feeds_override is None:
                cls._data_feeds = [CoinCapDataFeed.get_instance(), CoinGeckoDataFeed.get_instance()]
            else:
                cls._data_feeds = cls._data_feeds_override
            # Set default rate and source for token rates globally
            fetcher_global_config: List[List[str, str]] = global_config_map["exchange_rate_fetcher"].value or []
            # Set rate and source for tokens that needs conversion, overwrites global config
            rate_conversion_config: List[List[str, str, str]] = global_config_map[
                                                                    "exchange_rate_conversion"].value or []

            if cls._exchange_rate_config_override is None:
                conversion_required = {e[0]: {"default": e[1], "source": e[2]} for e in rate_conversion_config}
                global_config = {e[0]: {"default": NaN, "source": e[1]} for e in fetcher_global_config}
            else:
                conversion_required = cls._exchange_rate_config_override.get("conversion_required", {})
                global_config = cls._exchange_rate_config_override.get("global_config", {})

            global_config = {k.upper(): v for k, v in global_config.items()}
            conversion_required = {k.upper(): v for k, v in conversion_required.items()}
            cls._exchange_rate_config = {
                "conversion_required": conversion_required,
                "global_config": {**global_config, **conversion_required}
            }
            cls._exchange_rate = {k: v["default"] for k, v in cls._exchange_rate_config["global_config"].items()}

        except Exception:
            cls.logger().error("Error initiating config for exchange rate conversion.", exc_info=True)

    @property
    def all_exchange_rate(self) -> Dict[str, float]:
        return self._all_data_feed_exchange_rate.copy()

    @property
    def exchange_rate(self) -> Dict[str, float]:
        return self._exchange_rate.copy()

    def get_exchange_rate(self, source: str = None):
        if source == "default":
            if self.DEFAULT_DATA_FEED_NAME not in self.all_exchange_rate:
                self.logger().error(f"{self.DEFAULT_DATA_FEED_NAME} is not in one of the data feeds: "
                                    f"{self.all_exchange_rate.keys()}.")
                raise Exception("Data feed name not valid.")
            return self.all_exchange_rate[self.DEFAULT_DATA_FEED_NAME]

        elif source in self.all_exchange_rate.keys():
            return self.all_exchange_rate[source]

        elif source == "config":
            return self.exchange_rate
        else:
            raise Exception("Source name for exchange rate is not valid.")

    def __init__(self):
        self._fetch_exchange_rate_task: Optional[asyncio.Task] = None
        self.init_config()

    def adjust_token_rate(self, asset_name: str, price: float, source: str = None) -> float:
        """
        Returns the USD rate of a given token if it is found in conversion_required config
        :param source:
        :param asset_name:
        :param price:
        :return:
        """
        asset_name = asset_name.upper()
        if not self._started:
            self.start()
        exchange_rate = self.get_exchange_rate(source)
        if asset_name in self._exchange_rate_config["conversion_required"] and asset_name in self._exchange_rate:
            return exchange_rate[asset_name] * price
        else:
            return price

    def convert_token_value(self, amount: float, from_currency: str, to_currency: str, source: str = None):
        """
        Converts a token amount to the amount of another token with equivalent worth
        :param source:
        :param amount:
        :param from_currency:
        :param to_currency:
        :return:
        """
        if not self._started:
            self.start()

        exchange_rate = self.get_exchange_rate(source)

        from_currency = from_currency.upper()
        to_currency = to_currency.upper()
        # assume WETH and ETH are equal value
        if from_currency == "ETH" and to_currency == "WETH" or from_currency == "WETH" and to_currency == "ETH":
            return amount
        from_currency_usd_rate = exchange_rate.get(from_currency, NaN)
        to_currency_usd_rate = exchange_rate.get(to_currency, NaN)
        if math.isnan(from_currency_usd_rate) or math.isnan(to_currency_usd_rate):
            raise ValueError(f"Unable to convert '{from_currency}' to '{to_currency}'. Aborting.")
        return amount * from_currency_usd_rate / to_currency_usd_rate

    async def update_exchange_rates_from_data_feeds(self):
        has_errors: bool = False
        try:
            for data_feed in self._data_feeds:
                self._all_data_feed_exchange_rate[data_feed.name] = data_feed.price_dict
            for data_feed in self._data_feeds:
                source_name = data_feed.name
                for asset_name, config in self._exchange_rate_config["global_config"].items():
                    asset_name = asset_name.upper()
                    if config["source"].lower() == source_name.lower():
                        price = data_feed.get_price(asset_name)
                        if price:
                            self._exchange_rate[asset_name] = price
                        else:
                            if self._show_update_exchange_rates_from_data_feeds_errors:
                                self.logger().network(
                                    f"No data found for {asset_name} in {source_name} data feed.",
                                    app_warning_msg=f"Asset data for {asset_name} not found in {source_name} data feed,"
                                                    f" please check your 'exchange_rate_conversion' configs."
                                )
                            has_errors = True
            if has_errors:
                # only show these errors once
                self._show_update_exchange_rates_from_data_feeds_errors = False

        except Exception:
            self.logger().warning(f"Error getting data from {source_name} data feed.", exc_info=True)
            raise

    async def wait_till_ready(self):
        for data_feed in self._data_feeds:
            try:
                await asyncio.wait_for(data_feed.get_ready(), timeout=self._data_feed_timeout)
            except asyncio.TimeoutError:
                if self._show_wait_till_ready_errors:
                    self.logger().warning(f"Error initializing data feed - {data_feed.name}.")
                    self._show_wait_till_ready_errors = False

    async def request_loop(self):
        while True:
            try:
                await self.wait_till_ready()
                await self.update_exchange_rates_from_data_feeds()
                self._ready_notifier.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error(f"Error sending requests.", exc_info=True, extra={"do_not_send": True})

            await asyncio.sleep(self._update_interval)

    def start(self):
        self.stop()
        for data_feed in self._data_feeds:
            if not data_feed.started:
                data_feed.start()
        self._fetch_exchange_rate_task = asyncio.ensure_future(self.request_loop())
        self._started = True

    def stop(self):
        for data_feed in self._data_feeds:
            data_feed.stop()
        if self._fetch_exchange_rate_task and not self._fetch_exchange_rate_task.done():
            self._fetch_exchange_rate_task.cancel()
        self._started = False

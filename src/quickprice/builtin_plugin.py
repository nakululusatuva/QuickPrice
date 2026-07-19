"""The instruments shipped with QuickPrice itself."""

from __future__ import annotations

from .equities import COMMON_STOCKS, QUARTERLY_DIVIDEND_STRATEGY, CommonStockMetadata
from .fx import FX_CURRENCY_NAMES, FX_SYMBOLS
from .plugin_api import (
    AssetClass,
    InstrumentPlugin,
    InstrumentSpec,
    MarketCalendar,
    RewardAccrualMode,
    YieldStrategy,
)


def _fx_instrument(symbol: str) -> InstrumentSpec:
    base, _, counter = symbol.partition(":")
    return InstrumentSpec(
        symbol=symbol,
        base=base,
        quote=counter,
        name=f"{FX_CURRENCY_NAMES[base]} / {FX_CURRENCY_NAMES[counter]}",
        description=(
            f"The value of one {FX_CURRENCY_NAMES[base]} expressed in {FX_CURRENCY_NAMES[counter]}."
        ),
        asset_class=AssetClass.FX,
        asset_type="forex_pair",
        price_basis="exchange_rate" if base == "USD" else "synthetic_cross",
        market_calendar=MarketCalendar.FX_24X5,
        stale_after_seconds=300.0 if symbol == "USD:CNH" else 1200.0,
        quote_poll_seconds=240.0,
    )


FX_INSTRUMENTS = tuple(_fx_instrument(symbol) for symbol in FX_SYMBOLS)


def _common_stock_instrument(metadata: CommonStockMetadata) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=metadata.symbol,
        base=metadata.ticker,
        quote="USD",
        name=metadata.name,
        description=metadata.description,
        asset_class=AssetClass.EQUITY,
        asset_type="common_stock",
        price_basis="last_trade",
        dividend_strategy=(
            QUARTERLY_DIVIDEND_STRATEGY if metadata.dividend_frequency == "quarterly" else None
        ),
        market_calendar=MarketCalendar.US_EQUITY,
        stale_after_seconds=120.0,
        quote_poll_seconds=5.0,
    )


COMMON_STOCK_INSTRUMENTS = tuple(_common_stock_instrument(item) for item in COMMON_STOCKS)


BUILTIN_PLUGIN = InstrumentPlugin(
    plugin_id="builtin",
    version="1.6.0",
    provider_installer="quickprice.providers.wiring:install_builtin_provider_routes",
    instruments=(
        InstrumentSpec(
            symbol="BTC:USDC",
            base="BTC",
            quote="USDC",
            name="Bitcoin",
            description="Bitcoin spot price quoted in USD Coin.",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            price_basis="last_trade",
            quote_poll_seconds=1.0,
        ),
        InstrumentSpec(
            symbol="ETH:USDC",
            base="ETH",
            quote="USDC",
            name="Ethereum",
            description="Ether spot price quoted in USD Coin.",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            price_basis="last_trade",
            quote_poll_seconds=1.0,
        ),
        InstrumentSpec(
            symbol="SOL:USDC",
            base="SOL",
            quote="USDC",
            name="Solana",
            description="Solana's native token spot price quoted in USD Coin.",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            price_basis="last_trade",
            quote_poll_seconds=1.0,
        ),
        InstrumentSpec(
            symbol="XMR:USDC",
            base="XMR",
            quote="USDC",
            name="Monero",
            description="Monero's privacy-focused native asset spot price quoted in USD Coin.",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            price_basis="last_trade",
            quote_poll_seconds=1.0,
        ),
        InstrumentSpec(
            symbol="POL:USDC",
            base="POL",
            quote="USDC",
            name="Polygon Ecosystem Token",
            description="Polygon's native ecosystem token spot price quoted in USD Coin.",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            price_basis="last_trade",
            quote_poll_seconds=1.0,
        ),
        InstrumentSpec(
            symbol="BNB:USDC",
            base="BNB",
            quote="USDC",
            name="BNB",
            description="BNB Chain's native token spot price quoted in USD Coin.",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            price_basis="last_trade",
            quote_poll_seconds=1.0,
        ),
        InstrumentSpec(
            symbol="TRX:USDC",
            base="TRX",
            quote="USDC",
            name="TRON",
            description="TRON's native token spot price quoted in USD Coin.",
            asset_class=AssetClass.CRYPTO,
            asset_type="spot_crypto",
            price_basis="last_trade",
            quote_poll_seconds=1.0,
        ),
        InstrumentSpec(
            symbol="WBETH:USDC",
            base="WBETH",
            quote="USDC",
            name="Wrapped Binance Beacon ETH",
            description=(
                "A value-accruing liquid-staking token whose market price is derived "
                "from synchronized component markets."
            ),
            asset_class=AssetClass.CRYPTO,
            asset_type="liquid_staking_token",
            price_basis="synthetic_cross",
            yield_strategy=YieldStrategy.STAKING_PROVIDER_METRIC,
            reward_accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
            underlying_asset="ETH",
            quote_poll_seconds=1.0,
        ),
        InstrumentSpec(
            symbol="BETH:USDC",
            base="BETH",
            quote="USDC",
            name="OKX Staked Ether",
            description=(
                "An OKX liquid-staking token representing staked Ether; rewards are "
                "distributed daily as additional BETH units."
            ),
            asset_class=AssetClass.CRYPTO,
            asset_type="liquid_staking_token",
            price_basis="synthetic_cross",
            yield_strategy=YieldStrategy.STAKING_PROVIDER_METRIC,
            reward_accrual_mode=RewardAccrualMode.DISTRIBUTED_UNITS,
            underlying_asset="ETH",
            quote_poll_seconds=1.0,
            history_poll_seconds=21_600.0,
        ),
        InstrumentSpec(
            symbol="STETH:USDC",
            base="STETH",
            quote="USDC",
            name="Lido Staked Ether",
            description=(
                "A rebasing liquid-staking token representing Ether staked through "
                "the Lido protocol; rewards accrue through balance rebases."
            ),
            asset_class=AssetClass.CRYPTO,
            asset_type="liquid_staking_token",
            price_basis="aggregated_spot_ratio",
            yield_strategy=YieldStrategy.STAKING_PROVIDER_METRIC,
            reward_accrual_mode=RewardAccrualMode.REBASING_BALANCE,
            underlying_asset="ETH",
            stale_after_seconds=1800.0,
            quote_poll_seconds=660.0,
            history_poll_seconds=21_600.0,
        ),
        InstrumentSpec(
            symbol="WSTETH:USDC",
            base="WSTETH",
            quote="USDC",
            name="Wrapped Lido Staked Ether",
            description=(
                "A non-rebasing wrapper around stETH whose unit value increases as "
                "Lido staking rewards accrue."
            ),
            asset_class=AssetClass.CRYPTO,
            asset_type="liquid_staking_token",
            price_basis="aggregated_spot_ratio",
            yield_strategy=YieldStrategy.STAKING_PROVIDER_METRIC,
            reward_accrual_mode=RewardAccrualMode.VALUE_ACCRUING,
            underlying_asset="ETH",
            stale_after_seconds=1800.0,
            quote_poll_seconds=660.0,
            history_poll_seconds=21_600.0,
        ),
        *COMMON_STOCK_INSTRUMENTS,
        InstrumentSpec(
            symbol="QQQM:USD",
            base="QQQM",
            quote="USD",
            name="Invesco NASDAQ 100 ETF",
            description="An exchange-traded fund tracking the NASDAQ-100 Index.",
            asset_class=AssetClass.EQUITY,
            asset_type="equity_etf",
            price_basis="last_trade",
            dividend_strategy="latest_regular_cash_annualized_x4",
            market_calendar=MarketCalendar.US_EQUITY,
            stale_after_seconds=120.0,
            quote_poll_seconds=5.0,
        ),
        InstrumentSpec(
            symbol="BOXX:USD",
            base="BOXX",
            quote="USD",
            name="Alpha Architect 1-3 Month Box ETF",
            description="An ETF seeking a Treasury-bill-like return through box spreads.",
            asset_class=AssetClass.BOND,
            asset_type="growth_bond_etf",
            price_basis="last_trade",
            yield_strategy=YieldStrategy.TREASURY_3M_PROXY_MINUS_EXPENSE,
            market_calendar=MarketCalendar.US_EQUITY,
            stale_after_seconds=120.0,
            quote_poll_seconds=5.0,
        ),
        InstrumentSpec(
            symbol="SGOV:USD",
            base="SGOV",
            quote="USD",
            name="iShares 0-3 Month Treasury Bond ETF",
            description="An ETF investing in United States Treasury bonds maturing within three months.",
            asset_class=AssetClass.BOND,
            asset_type="income_bond_etf",
            price_basis="last_trade",
            yield_strategy=YieldStrategy.LATEST_DISTRIBUTION_ANNUALIZED,
            market_calendar=MarketCalendar.US_EQUITY,
            stale_after_seconds=120.0,
            quote_poll_seconds=5.0,
        ),
        *FX_INSTRUMENTS,
    ),
)


def get_plugin() -> InstrumentPlugin:
    return BUILTIN_PLUGIN

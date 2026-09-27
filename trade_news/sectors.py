"""Sector and industry reference: our own two-level classification modelled on GICS/Morningstar.

GICS codes per company are paid data, so an equity's industry comes from its free SEC SIC code
(`classify_equity`), with per-ticker overrides where SIC misleads. Crypto follows DACS by
symbol, FX pairs get labels from their two currencies.

Keys are English slugs, unique across sectors and industries: the LLM returns them, the admin
filters by them. A key names either a sector or an industry (`expand` tells which).
"""

from __future__ import annotations

from dataclasses import dataclass

CYCLES = {"cyclical": "цикличный", "sensitive": "чувствительный", "defensive": "защитный"}


@dataclass(frozen=True)
class Industry:
    key: str
    name_ru: str


@dataclass(frozen=True)
class Sector:
    key: str
    name_ru: str
    cycle: str | None  # key of CYCLES; None for crypto and FX
    industries: tuple[Industry, ...]


def _s(key: str, name_ru: str, cycle: str | None, *industries: tuple[str, str]) -> Sector:
    return Sector(key, name_ru, cycle, tuple(Industry(k, n) for k, n in industries))


SECTORS: tuple[Sector, ...] = (
    _s("technology", "технологии", "sensitive",
       ("semiconductors", "полупроводники"),
       ("software", "софт и облака"),
       ("hardware", "железо и электроника"),
       ("it_services", "IT-услуги")),
    _s("communication", "связь и медиа", "sensitive",
       ("internet", "интернет-платформы и соцсети"),
       ("media", "медиа, стриминг и игры"),
       ("telecom", "телеком")),
    _s("consumer_cyclical", "товары не первой необходимости", "cyclical",
       ("autos", "автомобили и EV"),
       ("retail", "ритейл и e-commerce"),
       ("travel_leisure", "путешествия, отели, рестораны"),
       ("apparel_home", "одежда, роскошь, товары для дома")),
    _s("consumer_defensive", "товары первой необходимости", "defensive",
       ("food_beverage", "продукты и напитки"),
       ("household_tobacco", "бытовые товары и табак"),
       ("grocery_retail", "продуктовый ритейл")),
    _s("healthcare", "здравоохранение", "defensive",
       ("pharma", "фарма"),
       ("biotech", "биотех"),
       ("medtech", "медтехника"),
       ("health_services", "страховщики и сервисы здравоохранения")),
    _s("financials", "финансы", "cyclical",
       ("banks", "банки"),
       ("insurance", "страхование"),
       ("capital_markets", "брокеры, биржи, управляющие"),
       ("payments", "платёжные системы и финтех")),
    _s("industrials", "промышленность", "sensitive",
       ("aerospace_defense", "авиакосмос и оборона"),
       ("machinery", "машиностроение и оборудование"),
       ("transport", "транспорт и логистика"),
       ("construction", "строительство и инженерия")),
    _s("energy", "энергетика", "sensitive",
       ("oil_gas", "нефть и газ"),
       ("renewables", "возобновляемая энергетика")),
    _s("materials", "материалы", "cyclical",
       ("chemicals", "химия"),
       ("metals_mining", "металлы и добыча"),
       ("building_packaging", "стройматериалы и упаковка")),
    _s("real_estate", "недвижимость", "cyclical",
       ("reit", "REIT"),
       ("development", "девелопмент")),
    _s("utilities", "коммунальные услуги", "defensive"),
    _s("crypto", "крипта", None,
       ("cryptocurrencies", "криптовалюты"),
       ("smart_contracts", "платформы смарт-контрактов"),
       ("defi", "DeFi"),
       ("stablecoins", "стейблкоины"),
       ("crypto_other", "прочая крипта")),
    # A pair gets one of majors/emerging plus any number of the two labels after it.
    _s("fx", "валюты", None,
       ("fx_majors", "мажоры G10"),
       ("fx_emerging", "валюты развивающихся стран"),
       ("fx_commodity", "сырьевые валюты"),
       ("fx_safe_haven", "защитные валюты")),
)  # fmt: skip

_SECTOR_BY_KEY = {s.key: s for s in SECTORS}
_INDUSTRY_SECTOR = {i.key: s for s in SECTORS for i in s.industries}
_NAMES = {s.key: s.name_ru for s in SECTORS} | {
    i.key: i.name_ru for s in SECTORS for i in s.industries
}


def all_keys() -> list[str]:
    """Every sector and industry key, sector first then its industries (the LLM enum)."""
    return [k for s in SECTORS for k in (s.key, *(i.key for i in s.industries))]


def name_ru(key: str) -> str:
    return _NAMES[key]


def expand(key: str) -> tuple[str, str | None] | None:
    """'semiconductors' → ('technology', 'semiconductors'), 'utilities' → ('utilities', None),
    unknown → None."""
    if key in _SECTOR_BY_KEY:
        return key, None
    if key in _INDUSTRY_SECTOR:
        return _INDUSTRY_SECTOR[key].key, key
    return None


# --- equities: SEC SIC code -----------------------------------------------------------

# (first, last, key) inclusive, the first matching range wins: specific codes go before the
# range that contains them. A sector key means "sector known, no fitting industry".
_SIC_RANGES: tuple[tuple[int, int, str], ...] = (
    (100, 999, "food_beverage"),           # agriculture
    (1000, 1099, "metals_mining"),         # metal mining, incl. gold 1040
    (1200, 1299, "energy"),                # coal
    (1300, 1399, "oil_gas"),               # crude, gas, drilling, oilfield services
    (1400, 1499, "building_packaging"),    # stone, sand, gravel (aggregates)
    (1520, 1531, "development"),           # homebuilders
    (1500, 1799, "construction"),
    (2080, 2086, "food_beverage"),         # beverages
    (2000, 2099, "food_beverage"),
    (2100, 2199, "household_tobacco"),
    (2200, 2399, "apparel_home"),          # textiles, apparel
    (2400, 2499, "building_packaging"),    # lumber, wood
    (2500, 2599, "apparel_home"),          # furniture
    (2600, 2699, "building_packaging"),    # paper and packaging
    (2700, 2799, "media"),                 # publishing
    (2833, 2834, "pharma"),
    (2835, 2835, "medtech"),               # in vitro diagnostics
    (2836, 2836, "biotech"),
    (2840, 2844, "household_tobacco"),     # soap, cleaners, cosmetics
    (2800, 2899, "chemicals"),
    (2900, 2999, "oil_gas"),               # refining
    (3011, 3011, "autos"),                 # tires
    (3021, 3021, "apparel_home"),          # footwear (NKE)
    (3000, 3099, "chemicals"),             # rubber, plastics
    (3100, 3199, "apparel_home"),          # leather, footwear
    (3200, 3299, "building_packaging"),    # glass, cement, concrete
    (3300, 3399, "metals_mining"),         # steel, aluminium
    (3410, 3412, "building_packaging"),    # metal cans
    (3480, 3489, "aerospace_defense"),     # ordnance
    (3400, 3499, "machinery"),
    (3570, 3579, "hardware"),              # computers, storage, networking
    (3500, 3599, "machinery"),
    (3630, 3639, "apparel_home"),          # household appliances
    (3651, 3652, "hardware"),              # audio, video
    (3660, 3669, "hardware"),              # communications equipment
    (3674, 3674, "semiconductors"),
    (3670, 3679, "hardware"),              # other electronic components
    (3600, 3699, "machinery"),             # electrical equipment
    (3710, 3716, "autos"),
    (3720, 3729, "aerospace_defense"),     # aircraft and parts
    (3730, 3732, "aerospace_defense"),     # ship building
    (3743, 3743, "machinery"),             # railroad equipment
    (3760, 3769, "aerospace_defense"),     # missiles, space vehicles
    (3795, 3795, "aerospace_defense"),     # tanks
    (3700, 3799, "autos"),                 # motorcycles, RVs
    (3812, 3812, "aerospace_defense"),     # search, navigation, guidance
    (3820, 3829, "hardware"),              # measuring instruments
    (3840, 3851, "medtech"),
    (3860, 3861, "hardware"),              # photographic
    (3870, 3873, "apparel_home"),          # watches
    (3800, 3899, "hardware"),
    (3900, 3999, "apparel_home"),          # jewelry, toys, sporting goods
    (4600, 4619, "oil_gas"),               # pipelines
    (4700, 4729, "travel_leisure"),        # travel agencies (BKNG)
    (4000, 4799, "transport"),             # rail, trucking, shipping, airlines
    (4830, 4841, "media"),                 # broadcasting, cable TV
    (4800, 4899, "telecom"),
    (4922, 4923, "oil_gas"),               # natural gas transmission
    (4950, 4959, "industrials"),           # waste management
    (4900, 4999, "utilities"),
    (5045, 5045, "hardware"),              # computer distribution
    (5065, 5065, "hardware"),              # electronic parts distribution
    (5047, 5047, "health_services"),       # medical supplies distribution
    (5122, 5122, "health_services"),       # drug distribution
    (5140, 5149, "grocery_retail"),        # grocery wholesale
    (5170, 5172, "oil_gas"),               # petroleum products wholesale
    (5180, 5182, "food_beverage"),         # beverage wholesale
    (5000, 5199, "industrials"),           # trading companies and distributors
    (5331, 5331, "grocery_retail"),        # variety stores: WMT, COST, TGT, DG (as GICS)
    (5400, 5499, "grocery_retail"),        # food stores
    (5912, 5912, "grocery_retail"),        # drug stores
    (5810, 5813, "travel_leisure"),        # restaurants
    (5200, 5999, "retail"),
    (6020, 6036, "banks"),
    (6099, 6099, "payments"),
    (6000, 6169, "banks"),                 # incl. consumer credit, mortgage banks
    (6170, 6199, "financials"),            # 6199 "finance services" is a catch-all
    (6200, 6299, "capital_markets"),
    (6324, 6324, "health_services"),       # health plans (UNH)
    (6300, 6411, "insurance"),
    (6798, 6798, "reit"),
    (6792, 6792, "oil_gas"),               # oil royalty traders
    (6795, 6795, "metals_mining"),         # mineral royalty traders
    (6500, 6599, "development"),
    (6700, 6769, "financials"),            # holding offices (6770 blank checks stay unknown)
    (6790, 6799, "financials"),
    (7000, 7099, "travel_leisure"),        # hotels, casinos
    (7200, 7299, "consumer_cyclical"),     # personal services
    (7310, 7319, "media"),                 # advertising
    (7371, 7371, "it_services"),
    (7373, 7374, "it_services"),           # systems integration, data processing
    (7370, 7379, "software"),
    (7510, 7519, "transport"),             # car rental
    (7300, 7599, "industrials"),           # business services, staffing, 7389 NEC
    (7800, 7899, "media"),                 # film, video
    (7900, 7999, "travel_leisure"),        # amusement and recreation
    (8000, 8099, "health_services"),       # hospitals, labs
    (8200, 8299, "consumer_cyclical"),     # education
    (8711, 8711, "construction"),          # engineering services
    (8731, 8731, "biotech"),               # commercial biological research
    (8700, 8799, "industrials"),           # consulting, research, professional services
)  # fmt: skip

# SIC is too coarse or misleading for these (7389 "services NEC", 3674 shared with solar,
# 7370 shared with software).
_TICKER_OVERRIDES: dict[str, str] = {
    **dict.fromkeys(["V", "MA", "PYPL", "AXP", "FI", "FISV", "FIS", "GPN", "XYZ", "SQ"],
                    "payments"),
    **dict.fromkeys(["FSLR", "ENPH", "SEDG", "RUN"], "renewables"),
    **dict.fromkeys(["GOOGL", "GOOG", "META", "SNAP", "PINS", "RDDT"], "internet"),
    **dict.fromkeys(["EA", "TTWO", "RBLX"], "media"),
    **dict.fromkeys(["UBER", "LYFT"], "transport"),
    "ABNB": "travel_leisure",
    "EBAY": "retail",
    "BABA": "retail",
    "DIS": "media",
    "QCOM": "semiconductors",
    "IBM": "it_services",
    "AKAM": "it_services",
    "CVS": "health_services",
}  # fmt: skip


def classify_sic(sic: int | str | None) -> tuple[str | None, str | None]:
    """SEC SIC code → (sector, industry). (None, None) when unknown; industry may be None."""
    try:
        code = int(sic) if sic not in (None, "") else None
    except ValueError:
        code = None
    if code is None:
        return None, None
    for first, last, key in _SIC_RANGES:
        if first <= code <= last:
            return expand(key)  # type: ignore[return-value]  # table keys are checked in tests
    return None, None


def classify_equity(symbol: str, sic: int | str | None) -> tuple[str | None, str | None]:
    override = _TICKER_OVERRIDES.get(symbol.upper())
    if override:
        return expand(override)  # type: ignore[return-value]
    return classify_sic(sic)


# --- crypto: DACS ---------------------------------------------------------------------

_CRYPTO: dict[str, str] = {
    **dict.fromkeys(["BTC", "LTC", "XRP", "BCH", "XLM", "XMR"], "cryptocurrencies"),
    **dict.fromkeys(["ETH", "SOL", "ADA", "AVAX", "BNB", "TON", "TRX", "DOT", "SUI", "APT",
                     "NEAR"], "smart_contracts"),
    **dict.fromkeys(["UNI", "AAVE", "MKR", "CRV", "LDO", "HYPE"], "defi"),
    **dict.fromkeys(["USDT", "USDC", "DAI", "USDE", "PYUSD"], "stablecoins"),
    **dict.fromkeys(["DOGE", "SHIB", "PEPE", "LINK", "FET", "RENDER"], "crypto_other"),
}  # fmt: skip


def classify_crypto(symbol: str) -> tuple[str, str | None]:
    return "crypto", _CRYPTO.get(symbol.upper())


# --- FX -------------------------------------------------------------------------------

G10 = frozenset({"USD", "EUR", "JPY", "GBP", "CHF", "CAD", "AUD", "NZD", "NOK", "SEK"})
COMMODITY_CCY = frozenset({"AUD", "CAD", "NZD", "NOK"})
SAFE_HAVEN_CCY = frozenset({"JPY", "CHF"})


def fx_industries(base: str | None, quote: str | None) -> list[str]:
    """EURUSD → [fx_majors], AUDJPY → [fx_majors, fx_commodity, fx_safe_haven],
    USDTRY → [fx_emerging]."""
    ccys = {c.upper() for c in (base, quote) if c}
    if not ccys:
        return []
    out = ["fx_majors" if ccys <= G10 else "fx_emerging"]
    if ccys & COMMODITY_CCY:
        out.append("fx_commodity")
    if ccys & SAFE_HAVEN_CCY:
        out.append("fx_safe_haven")
    return out

"""Detect agent language from lead's city field."""

_MARATHI_CITIES = {
    "mumbai", "pune", "nagpur", "nashik", "aurangabad", "kolhapur", "solapur",
    "thane", "navi mumbai", "pimpri", "chinchwad", "ahmednagar", "satara",
    "sangli", "jalgaon", "amravati", "nanded", "latur", "dhule", "akola",
    "chandrapur", "parbhani", "ichalkaranji", "jalna", "ambernath", "bhiwandi",
    "panvel", "malegaon", "vasai", "virar", "mira", "bhayandar", "ulhasnagar",
    "kalyan", "dombivli", "badlapur", "raigad", "ratnagiri", "sindhudurg",
    "osmanabad", "beed", "hingoli", "washim", "buldhana", "yavatmal", "wardha",
    "gondiya", "bhandara", "gadchiroli",
}

_BENGALI_CITIES = {
    "kolkata", "howrah", "durgapur", "siliguri", "asansol", "kharagpur",
    "barasat", "bardhaman", "burdwan", "malda", "murshidabad", "midnapore",
    "medinipur", "haldia", "krishnanagar", "ranaghat", "habra", "raiganj",
    "balurghat", "purulia", "bankura", "bishnupur", "coochbehar", "alipurduar",
    "jalpaiguri", "darjeeling", "north 24 parganas", "south 24 parganas",
    "hooghly", "nadia", "birbhum", "behrampore", "kanchrapara", "naihati",
    "titagarh", "panihati", "kamarhati", "dum dum", "salt lake", "new town",
    "barrackpore", "serampore", "uttarpara", "chinsurah", "arambagh",
    "tamluk", "contai", "jhargram", "diamond harbour",
}


def detect_language(city: str) -> str:
    """Return 'marathi', 'bengali', or 'hinglish' based on city name."""
    if not city:
        return "hinglish"
    normalized = city.strip().lower()
    if normalized in _MARATHI_CITIES:
        return "marathi"
    if normalized in _BENGALI_CITIES:
        return "bengali"
    # Partial match for compound city names
    for mc in _MARATHI_CITIES:
        if mc in normalized or normalized in mc:
            return "marathi"
    for bc in _BENGALI_CITIES:
        if bc in normalized or normalized in bc:
            return "bengali"
    return "hinglish"

"""
categories.py — 5 category rulesets with tone rules and case anchors
"""

CATEGORIES = {
    "restaurant": {
        "tone": "Hinglish meme, appetite-driven, local social proof",
        "meme_allowed": True,
        "meme_phrases": [
            "bhai sun 👀",
            "yaar ek kaam kar",
            "setting ho gayi 🔥",
            "sigma merchant move fr",
            "no cap ye try kar",
            "aaj toh chhappar phaad ke de 🚀",
            "bhai ye kya ho raha hai 💀",
            "ab toh banta hai yaar",
        ],
        "case_anchors": ["IPL match day", "corporate thali planning", "Tuesday dip"],
        "triggers": ["order_dip", "search_spike", "festival", "streak"],
        "avoid": "corporate language, multiple CTAs",
        "tone_instruction": (
            "Use Hinglish meme tone. Pick 1-2 meme phrases naturally. "
            "Order: FACT first → meme hook → CTA. Under 100 words."
        ),
    },
    "salon": {
        "tone": "aspirational, Hinglish ok, trend-aware, visual",
        "meme_allowed": True,
        "meme_phrases": [
            "bhai sun 👀",
            "yaar ek kaam kar",
            "setting ho gayi 🔥",
            "sigma merchant move fr",
            "no cap ye try kar",
            "aaj toh chhappar phaad ke de 🚀",
            "bhai ye kya ho raha hai 💀",
            "ab toh banta hai yaar",
        ],
        "case_anchors": ["bridal followup", "curious ask", "festival prep"],
        "triggers": ["festival", "recall", "streak", "research"],
        "avoid": "medical claims, heavy discount language",
        "tone_instruction": (
            "Use aspirational tone, Hinglish ok. Pick 1 meme phrase if natural. "
            "Order: FACT first → hook → CTA. Under 100 words."
        ),
    },
    "gym": {
        "tone": "motivational, streak-based, transformation",
        "meme_allowed": True,
        "meme_phrases": [
            "bhai sun 👀",
            "yaar ek kaam kar",
            "setting ho gayi 🔥",
            "sigma merchant move fr",
            "no cap ye try kar",
            "aaj toh chhappar phaad ke de 🚀",
            "bhai ye kya ho raha hai 💀",
            "ab toh banta hai yaar",
        ],
        "case_anchors": ["seasonal dip reframe", "customer lapse winback"],
        "triggers": ["order_dip", "recall", "festival", "streak"],
        "avoid": "body shaming, guilt language",
        "tone_instruction": (
            "Use motivational, streak-based tone. Hinglish meme ok. "
            "Order: FACT first → motivation hook → CTA. Under 100 words. "
            "Never use body shaming or guilt."
        ),
    },
    "dentist": {
        "tone": "clinical, trust-first, professional, reassuring",
        "meme_allowed": False,
        "meme_phrases": [],
        "case_anchors": ["research digest", "recall reminder", "seasonal checkup"],
        "triggers": ["search_spike", "recall", "research"],
        "avoid": "urgency pressure, casual language, memes",
        "tone_instruction": (
            "Use clinical, professional, trust-first tone. ZERO memes or Hinglish. "
            "Use precise numbers and formal CTAs. Under 100 words."
        ),
    },
    "pharmacy": {
        "tone": "utility-first, reliable, local, compliance-aware",
        "meme_allowed": False,
        "meme_phrases": [],
        "case_anchors": ["compliance alert", "chronic refill reminder"],
        "triggers": ["recall", "research", "seasonal"],
        "avoid": "fear-mongering, fake medical claims",
        "tone_instruction": (
            "Use utility-first, reliable, compliance-aware tone. ZERO memes. "
            "Be precise and helpful. Under 100 words."
        ),
    },
}


def get_category_rules(category: str) -> dict:
    """Return category ruleset. Defaults to restaurant if unknown."""
    key = category.lower().strip() if category else "restaurant"
    return CATEGORIES.get(key, CATEGORIES["restaurant"])


def get_all_category_names() -> list:
    return list(CATEGORIES.keys())

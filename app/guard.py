"""Investment-advice guard, layer 1: a deterministic check run BEFORE any model call (fast, free, high precision,
but it only knows the phrasings written into it; layer 2 is the model marker, see MARKER).

Refuses questions that ask for a recommendation or a prediction ("should I invest", "is it a good investment",
"will prices rise", "how much profit will I make"). It deliberately does NOT trigger on factual questions that merely
mention investing (e.g. "What's ROI, ROE and ROL according to DarGlobal?", "What investment opportunities does
DarGlobal offer?"), which the scraped pages can answer.
"""
import re

REFUSAL = (
    "I can't give investment advice, recommend a purchase, or predict prices or returns. "
    "I can tell you what DarGlobal and Wasalt publicly state about their projects, listings and prices; just ask for "
    "those facts. For decisions about buying or investing, please speak to a licensed financial adviser."
)

# Second layer (see app/main.py): the model is told to answer exactly "[[ADVICE]]" when a question asks for investment
# advice or a prediction, and the server swaps that for REFUSAL. It understands intent, which the patterns below do not.
MARKER = "[[ADVICE]]"

_ADVICE = [
    r"\bshould (i|we|you)\b.{0,40}\b(buy|invest|purchase|sell|put|choose|go for|pick|get)\b",
    r"\b(good|great|smart|wise|safe|safest|sound|solid|worthwhile|profitable|risky|bad|best|better) "
    r"(investment|investments|buy|bet|purchase|idea to (buy|invest))\b",
    r"\bworth (it|investing|buying|the (money|price|risk))\b",
    r"\b(good|right|best|bad|wrong|perfect) time (to|for) (buy|buying|invest|investing|sell|selling|purchase)\b",
    r"\b(will|would|is|are|going to|gonna|could)\b.{0,50}\b(prices?|values?|market|property|properties|rents?|it|they)\b"
    r".{0,40}\b(rise|go up|increase|appreciate|grow|fall|drop|decline|crash|double|go down)\b",
    r"\b(best|top|safest|most profitable) (project|property|properties|development|place|area|city|option)s?\b.{0,30}"
    r"\b(to )?(invest|buy|purchase)\b",
    r"\brecommend\b.{0,50}\b(invest|buy|purchase|property|project|villa|apartment|unit)",
    r"\bhow much (profit|return|money|income|gain)s?\b.{0,20}\b(can|will|would|could|do)\b.{0,10}\b(i|we|you)\b",
    r"\b(guarantee[sd]?|promise[sd]?)\b.{0,30}\b(returns?|profits?|yields?|appreciation)\b",
    r"\b(better|safer) (to )?(invest|buy|investment)\b|\bwhich .{0,40}\b(to invest in|to buy)\b",
    r"\b(financial|investment|buying) advice\b",
    # asking for our personal opinion or choice
    r"\bwould you\b.{0,30}\b(buy|invest|put|pick|choose|purchase|go for|recommend)\b",
    r"\bwhat would you do\b|\byour (honest )?(view|opinion|take|advice|recommendation)\b|\bdo you think\b.{0,60}\b(buy|invest|worth)",
    r"\b(am i|are we|would i be|would we be) (better off|wise|smart)\b",
    r"\bwhich\b.{0,40}\b(should|would|do) (i|we|you)\b.{0,20}\b(pick|choose|buy|invest|put|go for|take|prefer)\b",
    r"\bwhich should (i|we)\b|\bshould (i|we) (pick|choose)\b",
    r"\b(i|we) have\b.{0,25}\b(to spend|to invest|to put|budget)\b.{0,60}\b(which|where|what)\b",
    r"\b(would|will|is|does|do) (it|that|this)( be)? (smart|wise|sensible|a good idea|worth|good|bad)\b.{0,40}\b(buy|purchase|invest|sell|put)",
    # predictions and expectations of gains
    r"\b(will|would|do you think|going to|likely to|expect|expected to|might|could)\b.{0,60}\b(appreciate|make money|"
    r"turn a profit|pay off|rise in (value|price)|go up in (value|price)|double (in )?(price|value))\b",
    r"\b(can|could|should|do) (i|we) expect\b.{0,50}\b(price|value|return|profit|yield|rent|appreciat|gain|double)",
    r"\b(highest|best|maximum|top|biggest|greatest) (return|returns|roi|yield|profit|payoff|gain)s?\b",
    r"\bwhich\b.{0,40}\b(will|would)\b.{0,20}\b(give|earn|make|yield|return)\b.{0,20}\b(me|us|the most|more)\b",
    r"\b(is|are) (it|this|that|they|these|those|\w+( \w+){0,4}) (risky|too risky|safe|a buy|a sell|overpriced|undervalued|a bubble|"
    r"likely to (make|earn|return|profit|appreciate))\b",
    r"\b(stock|shares?|dar)\b.{0,25}\b(a buy|buy or sell|worth buying)\b|\ba buy right now\b|\bbuy or (wait|sell|hold|rent)\b",
    r"\b(put|invest|park|stash|lock)\b.{0,30}\b(my|our) (savings|money|cash|funds|capital)\b",
]
_ADVICE_RE = [re.compile(p, re.I) for p in _ADVICE]


# "Does Wasalt say Riyadh is a good investment?" asks what a page states (answerable), not for our opinion
_REPORTED = re.compile(r"\baccording to\b|\b(does|do|did|is|are)\b.{0,25}\b(say|says|state|states|claim|claims|mention|"
                       r"mentions|describe|describes|call|calls|advertise|advertises)\b", re.I)
_PERSONAL = re.compile(r"\bshould (i|we|you)\b|\brecommend", re.I)


def is_investment_advice(question: str) -> bool:
    q = question.strip()
    if _REPORTED.search(q) and not _PERSONAL.search(q):
        return False
    return any(r.search(q) for r in _ADVICE_RE)

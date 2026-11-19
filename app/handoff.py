"""Lead capture / handoff: recognise "I want to buy / book a viewing / talk to someone" and offer a next step.

Two layers, like the advice guard: (1) regex for obvious phrasings, (2) the model is told to start its reply with
HANDOFF when the user shows buying or enquiry intent (it understands phrasings the patterns miss). The marker is
stripped before the user sees the reply. A false positive only shows a small card; a miss costs nothing because the
"Enquire" button is always available.

Contact links are the companies' own public pages. The enquiry form saves to this demo's own database: it does NOT
reach DarGlobal or Wasalt (the UI says so).
"""
import re

HANDOFF = "[[HANDOFF]]"

CONTACTS = {
    "dar": {"company": "DarGlobal", "url": "https://darglobal.co.uk/get-in-touch"},
    "wasalt": {"company": "Wasalt", "url": "https://wasalt.sa/en"},
}

_INTENT = [
    r"\b(i|we)\b.{0,14}\b(want|wish|would like|'d like|need|plan|intend|am looking|are looking|'m looking|am interested|"
    r"are interested|'m interested|hope)\b.{0,28}\b(buy|buying|purchase|purchasing|rent|renting|reserve|reserving|book|view|"
    r"visit|own|invest in|get a|find a|move|enquire|inquire)\b",
    r"\binterested in (buying|purchasing|renting|reserving|owning|a (villa|apartment|flat|penthouse|home|property|unit|plot|"
    r"land|mansion|residence)|one of)",
    r"\b(book|arrange|schedule|set up|organi[sz]e|request) (a |an |my |the )?(viewing|visit|tour|call|meeting|consultation|"
    r"appointment|callback|site visit)\b",
    r"\b(speak|talk|chat|connect|get)( me)?( in touch)? (to|with) (a |an |the )?(someone|somebody|agent|person|human|sales|"
    r"adviser|advisor|representative|consultant|broker|team|salesperson|manager)\b",
    r"\b(contact|call|email|reach|message|phone) (me|us|sales|an agent|the sales|someone|the team|a broker)\b",
    r"\bhow (do|can|could|would|should) (i|we)\b.{0,15}\b(buy|purchase|reserve|book|register|get started|enquire|inquire|"
    r"make an offer|pay a deposit|put down a deposit)",
    r"\b(register|express|leave) (my |our )?(interest|details)\b|\bmake an? (offer|enquiry|inquiry)\b|\bget in touch\b|"
    r"\bcall (me|us) back\b|\bcall me\b|\bsend me (a |the )?(brochure|price ?list|details|information|factsheet|floor ?plans?)\b",
]
_INTENT_RE = [re.compile(p, re.I) for p in _INTENT]


def wants_handoff(question: str) -> bool:
    return any(r.search(question) for r in _INTENT_RE)


def contacts_for(question: str, sites=()):
    """both companies' contact pages, the one the conversation is about first"""
    q = question.lower()
    dar = bool(re.search(r"\bdar\b|darglobal|dar global", q))
    was = "wasalt" in q
    first = "dar" if dar and not was else "wasalt" if was and not dar else (
        "wasalt" if sites and sites[0] == "wasalt" else "dar")
    return [CONTACTS[first], CONTACTS["wasalt" if first == "dar" else "dar"]]

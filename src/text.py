START_TEXT = (
    "Hi! I can read *public* info from Coursera links and show a quick summary.\n\n"
    "Send a Coursera link (course / specialization / professional certificate).\n\n"
    "Examples:\n"
    "https://www.coursera.org/professional-certificates/adp-airs-entry-level-recruiter\n"
    "https://www.coursera.org/specializations/jhu-data-science\n"
    "https://www.coursera.org/learn/intro-fpga-design-embedded-systems/home/welcome\n\n"
    "⏳ First request may take up to 1 minute.\n"
    "Commands: /start /help"
)

HELP_TEXT = (
    "*How it works*\n"
    "- Send a Coursera link\n"
    "- I fetch the public page and try to extract title / type / total hours\n\n"
    "*Notes*\n"
    "- Best-effort: Coursera may change page layout\n"
    "- No login, no payments, no certificates\n"
)

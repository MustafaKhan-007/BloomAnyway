"""What a brand-new landing page starts out as.

Content, not code. Each template is a list of blocks written out in the same
shape the builder saves them in — a type, the fields that differ from that
block's defaults, and its repeating items. ``landing_pages`` turns them into
real blocks and runs them through the same cleaner a save goes through, so
nothing here can put anything on a page that typing it in wouldn't.

Deliberately a separate file. The challenge page below is several hundred
lines of somebody's actual words, and burying the twenty lines of machinery
that build a page under them helps nobody.

Every word here is a starting point. The owner opens the page and rewrites
whatever she likes; none of it is live until she publishes.
"""
from __future__ import annotations

# --- the two-month challenge --------------------------------------------------

#: Small mono capitals, the lettering the mockup uses for every label that
#: sits under or above something bigger. Written out as a span because that
#: is all it is — anyone can add or remove it from the panel on the right.
_CAPS = '<span class="lp-t--mono lp-t--caps lp-t--small">%s</span>'

#: Laid out to the mockup: a left-aligned hero on a wash with her numbers
#: beside it, a dark band of figures, and then sections alternating between
#: cream and blush with two of them split into a column of words and a
#: panel of proof.
_CHALLENGE_BLOCKS = [
    # The hero takes seven of the twelve columns and the cards take the
    # other five, so they sit on one row. Both are on the same wash, and
    # the wash runs straight down, so the join between them is invisible.
    {"type": "hero", "fields": {
        "eyebrow": "Round 2 · Enrollment opening soon",
        "heading": "You don't need an audience.<br>"
                   "<span class=\"lp-t--blush\"><em>You need a plan.</em></span>",
        "body":
            "<p>I built this 2-month challenge for moms like me — "
            "stay-at-home moms, single moms, housewives, and women working "
            "full-time jobs who are ready to build a real income online. No "
            "experience needed, I'll show you exactly how, step by step, the "
            "way I wish someone had shown me.</p>"
            "<p><span class=\"lp-t--mono lp-t--small\">2 months of live "
            "access · Bloom Anyway community included</span></p>",
        "button_text": "Get on the Waitlist →",
        "button_url": "#waitlist",
        "button2_text": "See what's inside",
        "button2_url": "#curriculum",
        "height": "tall", "align": "left", "bg": "gradient",
        "col_start": 1, "col_span": 7,
    }},

    # Her screenshots, as cards. Five columns is narrower than the point at
    # which a row of three becomes a row of three, so they stack — which is
    # how they are stacked in the mockup.
    {"type": "features", "fields": {
        "heading": "", "body": "", "columns": "3", "card_style": "raised",
        "align": "left", "pad": "large", "bg": "gradient",
        "col_start": 8, "col_span": 5,
    }, "items": [
        {"title": "Reel insights",
         "body": "<p><strong>1.18M</strong> views · <strong>940</strong> "
                 "follows</p>"},
        {"title": "Student result",
         "body": "<p><strong>815.9K</strong> viewers · <strong>32.1s</strong> "
                 "average watch</p>"},
        {"title": "My growth",
         "body": "<p><strong>0 → 170K</strong> in <strong>7 months</strong></p>"},
    ]},

    # The labels are set in small mono capitals so the four figures above
    # them carry the band on their own.
    {"type": "stats", "fields": {"bg": "dark", "pad": "medium"}, "items": [
        {"value": "5-figure", "label": _CAPS % "Months since month 4"},
        {"value": "0 → 170K", "label": _CAPS % "Followers in 7 months"},
        {"value": "2", "label": _CAPS % "Months of live access"},
        {"value": "Weekly", "label": _CAPS % "Task sheets + reel reviews"},
    ]},

    # Four cards two across, not a bulleted list: each one is a whole
    # thought and they read as four doors rather than one paragraph.
    {"type": "features", "fields": {
        "eyebrow": "Is this you",
        "heading": "I built this for women juggling a lot more than a "
                   "content calendar",
        "body": "", "columns": "2", "card_style": "raised", "align": "left",
        "pad": "large", "bg": "cream",
    }, "items": [
        {"icon": "＊", "title": "",
         "body": "You've never posted content before and don't know where to "
                 "even start — camera, editing, none of it."},
        {"icon": "＊", "title": "",
         "body": "You're working full-time, raising kids, or both, and need "
                 "a plan that fits into a busy life, not around it."},
        {"icon": "＊", "title": "",
         "body": "You want more than “just post consistently” — you want the "
                 "actual strategy behind what works."},
        {"icon": "＊", "title": "",
         "body": "You want to turn content into real income: affiliates, "
                 "paid collabs, or your own digital product."},
    ]},

    # The figure and the line she wants remembered, as a dark column beside
    # the story rather than a banner of its own between sections.
    {"type": "text", "fields": {
        "heading": "5-fig/mo",
        "body":
            "<p><span class=\"lp-t--mono lp-t--small lp-t--caps\">From "
            "digital products, since month 4</span></p>"
            "<p><em>“I built this while working full-time and raising my kid "
            "alone. This isn't theory — it's exactly what I did, and I want "
            "to hand it to you.”</em></p>",
        "pad": "large", "bg": "plum", "col_start": 1, "col_span": 5,
    }},

    {"type": "text", "fields": {
        "eyebrow": "Why I'm doing this",
        "heading": "I'm a single mom. I built this from zero, working "
                   "full-time. Now I want to hand you the roadmap.",
        "body":
            "<p>I'm not teaching from theory. <strong>I went from 0 to "
            "170,000 followers in 7 months</strong> while working a "
            "full-time job and raising my kid on my own, and I've been "
            "making <strong>five figures a month from digital products "
            "since my 4th month</strong> as a creator — learning it all in "
            "real time, with no team and no big following to start from.</p>"
            "<p>I'm here for the moms, the stay-at-home moms, the "
            "housewives, and the women clocking into a 9-to-5 who know "
            "they're capable of more but don't know where to start. I've "
            "been you. I built this challenge to be the exact roadmap I wish "
            "someone had handed me — broken into a pace a busy woman can "
            "actually keep up with.</p>"
            "<p>After about 7 months of consistency, I left my full-time job "
            "for good. Today that same business funds multiple vacations a "
            "year and a six-figure investment account — and my whole goal "
            "now is helping other women get there too.</p>",
        "pad": "large", "bg": "blush", "col_start": 6, "col_span": 7,
    }},

    # The hero's second button jumps here.
    {"type": "features", "fields": {
        "eyebrow": "The curriculum",
        "heading": "Four stages, zero to income",
        "body": "Two months, broken into a clear progression — from setting "
                "up your account to knowing what to do with the money once "
                "it starts coming in. This is the exact path I took, laid "
                "out so you don't have to guess.",
        "columns": "4", "card_style": "outlined", "align": "left",
        "pad": "large", "bg": "plum", "anchor": "curriculum",
    }, "items": [
        {"title": "<span class=\"lp-t--mono lp-t--small\">Stage 01</span>"
                  "<br><em>Foundations</em>",
         "body": "<ul><li>Account setup</li>"
                 "<li>Cross-posting to platforms</li>"
                 "<li>Niche selection</li></ul>"},
        {"title": "<span class=\"lp-t--mono lp-t--small\">Stage 02</span>"
                  "<br><em>Create</em>",
         "body": "<ul><li>Speaking to the camera</li><li>Filming</li>"
                 "<li>Editing</li><li>Content ideas</li></ul>"},
        {"title": "<span class=\"lp-t--mono lp-t--small\">Stage 03</span>"
                  "<br><em>Grow</em>",
         "body": "<ul><li>Engagement strategy</li>"
                 "<li>Building your personal brand</li></ul>"},
        {"title": "<span class=\"lp-t--mono lp-t--small\">Stage 04</span>"
                  "<br><em>Earn</em>",
         "body": "<ul><li>Affiliates &amp; paid collabs</li>"
                 "<li>Digital products</li>"
                 "<li>Personal brand &amp; how to sell</li>"
                 "<li>Investing basics</li></ul>"},
    ]},

    {"type": "text", "fields": {
        "eyebrow": "The part most courses skip",
        "heading": "Most creators can build a product. Almost none know how "
                   "to sell it.",
        "body":
            "<p>Building a personal brand is one thing — <strong>knowing how "
            "to actually talk to your audience so they buy</strong> is a "
            "completely different skill. It's the piece most courses leave "
            "out, or sell separately for a premium.</p>"
            "<p>I'm teaching you both: how to build a personal brand people "
            "trust, and how to market and sell to that audience in a way "
            "that actually converts — the exact approach behind my own "
            "five-figure months.</p>",
        "pad": "large", "bg": "cream", "col_start": 1, "col_span": 7,
    }},

    {"type": "stats", "fields": {
        "eyebrow": "My track record", "pad": "large", "bg": "white",
        "panel": "card", "col_start": 8, "col_span": 5,
    }, "items": [
        {"value": "5 figures", "label": "Monthly revenue"},
        {"value": "Month 4", "label": "Earning since"},
        {"value": "Month 7", "label": "Left my full-time job"},
        {"value": "6 figures", "label": "Investment account"},
    ]},

    {"type": "features", "fields": {
        "eyebrow": "What's included",
        "heading": "You won't be figuring this out alone",
        "body": "", "columns": "3", "card_style": "raised", "align": "left",
        "pad": "large", "bg": "soft",
    }, "items": [
        {"icon": "✦", "title": "Bloom Anyway community",
         "body": "2 months of access to a private community of women "
                 "learning and building alongside you — ask questions, get "
                 "support, celebrate wins."},
        {"icon": "↻", "title": "Reel reviews",
         "body": "I review your reels directly and break down what's working "
                 "in my own content, so you can see the strategy applied in "
                 "real time."},
        {"icon": "▤", "title": "Weekly task sheets",
         "body": "No guessing what to do next. Each week comes with a clear "
                 "task sheet so the plan fits into a busy schedule instead "
                 "of taking it over."},
        {"icon": "◈", "title": "The showcase",
         "body": "A shared showcase to advertise your products — built "
                 "especially for creators with a smaller following to get in "
                 "front of an audience."},
        {"icon": "●", "title": "Optional 1:1 coaching",
         "body": "Work directly with me for personalized guidance on your "
                 "account, content, and strategy."},
        {"icon": "◐", "title": "Investing basics",
         "body": "Once the income starts, a lesson on what to actually do "
                 "with it — because building income is only half the plan."},
    ]},

    # Her words first, then the figures under a hairline. The card's picture
    # slot is where the screenshot those figures came from goes.
    {"type": "features", "fields": {
        "eyebrow": "Real results",
        "heading": "From women who started exactly where you are",
        "body": "", "columns": "3", "card_style": "raised", "align": "left",
        "pad": "large", "bg": "blush",
    }, "items": [
        {"title": "",
         "body": "<p><em>“Thank you for giving me a roadmap that helped me "
                 "structure my thoughts and create videos like this.”</em></p>",
         "note": "24.4K views · 339 new follows"},
        {"title": "",
         "body": "<p><em>“Can't thank you enough for pushing me and "
                 "inspiring me. It's not a big number compared to where I "
                 "want to reach, but it feels like a real "
                 "achievement.”</em></p>",
         "note": "1.19M views · 940 new follows"},
        {"title": "",
         "body": "<p><em>“Thank u for guiding us so well — these kinds of "
                 "results keep me motivated. There's not a single day I'm "
                 "regretting taking your challenge.”</em></p>",
         "note": "29.7K views · 3.1K likes"},
        {"title": "",
         "body": "<p><em>“I went through all your course work and it "
                 "honestly is soooo helpful and in depth. Just posted my "
                 "first reel — so excited for this journey!”</em></p>"},
        {"title": "",
         "body": "<p><em>“Woke up to this! What you've taught, the little "
                 "tips and tricks — everything is so on point. We can tell "
                 "you put your heart and soul into it.”</em></p>"},
        {"title": "",
         "body": "<p><em>“Enjoying little wins, and focusing on staying "
                 "consistent. Thank you for all your guidance!”</em></p>"},
    ]},

    {"type": "faq", "fields": {
        "eyebrow": "Questions", "heading": "Before you join",
        "style": "folded", "pad": "large", "bg": "cream",
    }, "items": [
        {"question": "Do I need an existing audience?",
         "answer": "No. This challenge is built to take you from zero — no "
                   "following, no experience, no content posted yet."},
        {"question": "I work full-time and have kids — will I actually have "
                     "time for this?",
         "answer": "That is who it was built for. Each week is one task "
                   "sheet, and everything is recorded, so you work through "
                   "it at whatever hour you have."},
        {"question": "What if I have no idea what to film?",
         "answer": "Stage two is that, and nothing else: speaking to the "
                   "camera, filming, editing, and where the ideas come from."},
        {"question": "How is this different from free advice online?",
         "answer": "Free advice tells you to post consistently. This is the "
                   "order I did it in, what I would skip, and how the money "
                   "actually arrives — with me looking at your reels along "
                   "the way."},
    ]},

    {"type": "text", "fields": {
        "eyebrow": "Why this is priced the way it is",
        "heading": "Consider this an investment, not an expense",
        "body":
            "<p>I spent close to $1,000 over the past year buying courses "
            "trying to piece this together myself. This challenge is "
            "everything it actually took me to get to five-figure months — "
            "taught by someone who knows stay-at-home moms and single moms "
            "don't have money to waste on courses that don't deliver.</p>",
        "align": "center", "width": "narrow", "pad": "medium", "bg": "cream",
    }},

    # A card sitting on the page rather than a band running across it, which
    # is what stops the last thing on the page reading as a footer.
    {"type": "cta", "fields": {
        "heading": "Round 2 is almost open.",
        "body": "Get on the waitlist to be first to know when I open "
                "enrollment — plus early access before it goes public. I "
                "can't wait to have you in here.",
        "button_text": "Join the Waitlist →",
        "button_url": "/challenge",
        "align": "center", "pad": "large", "bg": "dark", "panel": "card",
        "anchor": "waitlist",
    }},
]


# --- the page every landing page used to start as ------------------------------

#: What a new page was before there were templates at all, kept exactly so:
#: a hero, some figures, a few cards and a call to action, every one of them
#: holding its own placeholder words.
_CLASSIC_BLOCKS = [
    {"type": t} for t in
    ("hero", "stats", "text", "features", "image_text", "quote", "faq", "cta")
]


#: Every template, in the order the picker offers them. ``short`` is what the
#: picker itself can fit; ``hint`` is the longer line under it.
TEMPLATES: dict[str, dict] = {
    "challenge": {
        "label": "The challenge page",
        "short": "hero to waitlist",
        "hint": "The whole challenge page — hero, curriculum, what's "
                "included, real results, questions and a waitlist.",
        "title": "Two Month Challenge",
        "blocks": _CHALLENGE_BLOCKS,
    },
    "classic": {
        "label": "Classic starter",
        "short": "hero, cards, questions",
        "hint": "The page new ones used to start as: one of each kind of "
                "block, filled with placeholder words to write over.",
        "title": "Untitled landing page",
        "blocks": _CLASSIC_BLOCKS,
    },
    "blank": {
        "label": "Blank page",
        "short": "nothing yet",
        "hint": "An empty canvas. Add the blocks you want, in the order you "
                "want them.",
        "title": "Untitled landing page",
        "blocks": [],
    },
}

#: What the New button starts a page as when nothing else is asked for.
DEFAULT_TEMPLATE = "challenge"

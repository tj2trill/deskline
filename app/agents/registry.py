"""The team roster: who does what, how often, how autonomous by default, and their knobs."""
from .base import Knob
from .team import (AccountManager, BillingGuardian, Closer, DailyBrief, DemoBuilder, OpsMonitor, Outreach, Prospector,
                   QualityCoach, Strategist)

TEAM = [
    Prospector(
        key="prospector", title="Prospector", icon="search", default_mode="auto", interval_minutes=1440,
        role="Searches Google Maps for the businesses you target, scores them, and checks their websites. Searches the targets that bring paying clients first.",
        knobs=[
            Knob("targets", "What to search", "lines", [], "One per line: type of business | city. For example: plumber | Hamilton, ON"),
            Knob("searches_per_run", "Searches per day", "int", 2, "Each search returns up to 60 businesses."),
            Knob("enrich_top", "Websites to check per day", "int", 15, "Finds emails, chat widgets and online booking."),
            Knob("research_after_days", "Search the same place again after (days)", "int", 30),
            Knob("retired", "Retired targets", "lines", [], "Targets the Strategist found don't work. Move a line back up to try it again."),
        ]),
    DemoBuilder(
        key="demo_builder", title="Demo builder", icon="bot", default_mode="approve", interval_minutes=60,
        acts=["buy_number"],
        role="Turns the best prospects into a live demo receptionist trained on their own website, plus a demo site.",
        knobs=[
            Knob("min_score", "Minimum lead score", "int", 60),
            Knob("require_email", "Only prospects with an email", "bool", True, "So outreach can reach them."),
            Knob("max_per_run", "Demos per run", "int", 3),
            Knob("max_per_day", "Demos per day", "int", 10, "Each demo uses an LLM build and, if approved, a phone number."),
            Knob("build_site", "Also build a demo website", "bool", True),
            Knob("get_number", "Get each demo a phone number", "bool", True, "Numbers come from Vapi in the prospect's area code."),
        ]),
    Outreach(
        key="outreach", title="Outreach", icon="send", default_mode="approve", interval_minutes=30,
        acts=["send_email_draft"],
        role="Writes a 3-email sequence for each prospect and sends it, with follow-ups, inside your window and daily cap. Uses the email angles that get replies.",
        knobs=[
            Knob("min_score", "Minimum lead score", "int", 50),
            Knob("require_demo", "Wait until their demo number is ready", "bool", True, "Emails then include a number they can call."),
            Knob("window_start", "Send from", "time", "09:00", "In your dashboard timezone."),
            Knob("window_end", "Send until", "time", "16:30"),
            Knob("weekdays_only", "Weekdays only", "bool", True),
            Knob("max_per_run", "Emails per run", "int", 6, "Spreads sending out through the day. The daily cap is in Settings."),
        ]),
    Closer(
        key="closer", title="Closer", icon="billing", default_mode="approve", interval_minutes=60,
        acts=["send_checkout", "send_checkout_reminder", "onboard_owner"],
        role="When you mark a prospect won, sends the checkout link, follows up if it isn't paid, then onboards the owner.",
        knobs=[
            Knob("reminder_days", "Remind after (days)", "int", 3),
            Knob("max_reminders", "Reminders at most", "int", 2),
        ]),
    QualityCoach(
        key="quality_coach", title="Quality coach", icon="lightbulb", default_mode="approve", interval_minutes=30,
        acts=["publish_answer", "add_instruction", "email_owner"],
        role="Answers repeated caller questions from the client's own information and suggests fixes for calls that went wrong.",
        knobs=[
            Knob("min_times_asked", "Act on questions asked at least", "int", 2),
            Knob("ask_owner", "Ask the owner when the answer isn't in their info", "bool", True),
            Knob("coach_calls", "Suggest fixes for flagged calls", "bool", True),
            Knob("max_per_run", "Items per run", "int", 10),
        ]),
    AccountManager(
        key="account_manager", title="Account manager", icon="user", default_mode="approve", interval_minutes=720,
        acts=["email_owner"],
        role="Watches every client for silent lines and falling call volume, and checks in before they churn.",
        knobs=[
            Knob("silent_days", "Flag a client with no calls for (days)", "int", 3),
            Knob("drop_pct", "Flag a week-over-week drop of (%)", "int", 60),
        ]),
    BillingGuardian(
        key="billing_guardian", title="Billing guardian", icon="shield", default_mode="approve", interval_minutes=60,
        acts=["pause_client"],
        role="Makes sure every billable minute reaches Stripe and pauses clients who stay unpaid.",
        knobs=[Knob("pause_after_days", "Pause after unpaid for (days)", "int", 7)]),
    OpsMonitor(
        key="ops_monitor", title="Ops monitor", icon="alert", default_mode="auto", interval_minutes=15,
        role="Watches for webhook errors, failing agents and broken connections, and alerts you.",
        knobs=[
            Knob("error_threshold", "Alert at this many problems in an hour", "int", 5),
            Knob("health_every_minutes", "Check Vapi and Stripe every (minutes)", "int", 60),
        ]),
    DailyBrief(
        key="daily_brief", title="Daily brief", icon="file", default_mode="auto", interval_minutes=30,
        role="Emails you a morning summary: calls, bookings, pipeline, and what's waiting for your approval.",
        knobs=[Knob("hour", "Send at (hour, your timezone)", "int", 7)]),
]

TEAM.append(Strategist(
    key="strategist", title="Strategist", icon="sparkles", default_mode="approve", interval_minutes=360,
    acts=["apply_setting", "set_mode"],
    role="Measures what works and what makes money (targets, email angles, demo lines, timing, clients, niches) "
         "from real replies, wins and payments, and proposes the changes the evidence supports.",
    knobs=[
        Knob("min_sample", "Emails needed before judging", "int", 20, "Fewer than this and a result is called too early."),
        Knob("confidence", "How sure before calling a result (%)", "int", 90, "90 means a 1-in-10 chance of a wrong call."),
        Knob("retire_targets", "Propose retiring targets that don't work", "bool", True),
        Knob("propose_autopilot", "Propose Autopilot for agents you always approve", "bool", True),
    ]))

REGISTRY = {a.key: a for a in TEAM}

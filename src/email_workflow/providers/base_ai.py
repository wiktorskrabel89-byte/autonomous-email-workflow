from abc import ABC, abstractmethod
from typing import Optional
from email_workflow.models.email import EmailMessage
from email_workflow.models.analysis import (
    EmailAnalysis,
    DecisionSupportOutput,
    ReplyGenerationOutput,
    KnownFactsMerge,
    ReplyReviewOutput,
)
from email_workflow.models.state import ThreadState

# ---------------------------------------------------------------------------
# Prompt templates
#
# Design rules these follow:
#   1. The model judges; it never echoes data we already hold. message_id,
#      thread_id, sender and subject are merged back in by the caller, so the
#      model cannot corrupt them and we do not pay output tokens to repeat
#      them. That matters on a free API tier.
#   2. Every fact the model is told to reason about is actually supplied to it.
#   3. Rules are ordered and conflicts between them resolved explicitly, so the
#      model does not have to guess which rule wins.
#   4. Safety defaults are stated as hard stops, not preferences.
# ---------------------------------------------------------------------------

# How much of an email body is put in front of the model.
#
# A marketing newsletter is mostly markup and repetition: one real one came to
# 380,000 characters, roughly 95,000 tokens, and Groq refused the whole request
# with "context_length_exceeded". That is a bad_request, which does not fail
# over, so one fat advert could end a run.
#
# Nothing is lost by cutting it: what an email IS, and what it asks for, is
# settled in the first few thousand characters. Anything after that is footers,
# unsubscribe links and legal text.
MAX_BODY_CHARS = 12000


def trim_body(body: str, limit: int = MAX_BODY_CHARS) -> str:
    """The part of an email worth showing a model, and a note if there is more."""
    text = body or ""
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    left = len(text) - len(cut)
    return (
        f"{cut}\n\n[... {left:,} more characters of this message are not "
        f"shown. It was cut to fit the model's context window.]"
    )


CLASSIFICATION_PROMPT_TEMPLATE = """You triage one incoming email for a busy person and decide what should happen to it.

## What you are given
Everything you may rely on is below. If a detail is not in the email, the thread, or
the Known Facts, you do not know it. Never guess or invent it.

## Step 1 - Category
Pick exactly ONE that fits best:
personal, work, support, financial, shopping, receipt, newsletter, marketing,
notification, security, account, travel, calendar, automated, spam, other

Two of these are easy to confuse and the difference matters more than any other:
  financial = money that still needs a decision or an action from us: an invoice
              awaiting payment, a transfer to authorise, a charge to dispute,
              payment or bank details someone is asking for.
  receipt   = money already settled that needs nothing: a payment confirmation,
              a paid invoice, a subscription charge that went through.
Naming an amount or an invoice number does not make it financial. Being asked
for something does.

## Step 2 - Importance and urgency (these are different things)
importance = how much the consequences matter (low | medium | high)
urgency    = how soon it must be handled (low | medium | high)
A yearly tax deadline six weeks away is high importance, low urgency.
A colleague needing an answer before a 3pm meeting today is the reverse.

## Step 3 - Does it need anything?
action_required   = something must be DONE (pay, book, upload, decide, attend).
response_required = the sender is waiting for a REPLY from us.
A newsletter needs neither. A receipt usually needs neither.

## Step 3b - Is this written to US, or sent to a list?
personally_addressed = true when the email is about something OF OURS that we
did or have: our application, our order, our booking, our ticket, our account,
our interview. Someone on the other side acted on OUR thing.
It is false for bulk sends - a newsletter, an offer, a digest of listings, an
alert about opportunities that exist. Those go to thousands of people
unchanged, and our name at the top does not make one personal.

  "An employer has read your application"     -> true. It is about ours.
  "Last chance to send a CV for this role"    -> false. An advert.
  "Your parcel is out for delivery"           -> true.
  "Jobs we found that you might like"         -> false. A digest.

This is NOT the same question as the category. An employer replying about our
application is a notification AND personal. Getting this wrong files a reply to
something we did away with the supermarket newsletters, and it is never seen.

## Step 3c - Subjects this person never wants filed away
{protected_topics}
If the email is about one of those, copy that line into protected_topic exactly
as it is written above. If none of them fit, protected_topic is "".
Judge the subject matter, not the wording: the list is in their words, the mail
may be in any language.

## Step 3d - Which of their own labels does this belong under?
{your_labels}
Copy the label's name into suggested_label exactly as it is written above, or
"" if none of them fits. Judge it by what the email IS, using the description
next to each name - the names may be in one language and the mail in another.
Do not invent a label that is not on that list, and do not reach: an email in
the wrong folder is harder to find than one left where it was.

## Step 4 - Decide, using the first rule that matches
1. Category security, account or financial, or anything that ASKS us to pay,
   authorise, confirm or hand over: passwords, login codes, payment details,
   bank accounts, an invoice still awaiting payment, or a wire transfer ->
   "escalate", and safe_to_automate MUST be false. This overrides every rule
   below, even when the answer looks obvious and even when the Known Facts
   appear to cover it.
   A receipt for money already paid asks us for nothing, so it is NOT this rule
   - it is rule 6. Escalating those buries the ones that really do need a person.
2. The sender asks a question the Known Facts answer FULLY, and answering commits
   us to nothing new -> "automatically_reply", safe_to_automate true.
3. We could mostly answer, but a detail is missing or the reply would promise
   something (a price, a date, a deliverable) -> "create_draft".
4. Needs a human judgement call we cannot make from the facts -> "escalate".
5. Worth knowing about but needs no reply -> "notify_me".
6. Routine and safe to file away (receipts, notifications already acted on) -> "archive".
7. Bulk marketing, spam, or anything with no value -> "ignore".
Use "wait_for_approval" only when a reply is already drafted and the one thing
missing is a human saying yes.

## Step 5 - List what is missing
Put every fact you needed but did not have into missing_information. An empty list
is a strong claim: it means nothing was missing. Do not invent entries, and do not
empty it just to look decisive.
Put anything the email implies we have promised or will owe into commitments_implied.

## Step 6 - Confidence
Report real certainty, not enthusiasm.
0.9-1.0 unambiguous, 0.7-0.9 confident, 0.4-0.7 a plausible reading, below 0.4 a guess.
Below 0.7, prefer a safer decision (create_draft or escalate) over automatically_reply.
Low confidence plus an automatic reply is the one combination that causes real damage.

=== EMAIL ===
From:    {sender_name} <{sender_email}>
Known contact: {known_contact}
Date:    {received_at}
Subject: {subject}

{body}

=== THREAD SO FAR ===
{thread_context}

=== KNOWN FACTS (the only facts you may state as ours) ===
{known_facts}

=== OUTPUT ===
Reply with JSON only. No prose, no markdown fence. Exactly these keys:
{{
  "category": "<one category from Step 1>",
  "importance": "low|medium|high",
  "urgency": "low|medium|high",
  "action_required": true,
  "response_required": true,
  "safe_to_automate": false,
  "confidence": 0.0,
  "missing_information": [],
  "commitments_implied": [],
  "personally_addressed": false,
  "protected_topic": "",
  "suggested_label": "",
  "recommended_decision": "ignore|archive|notify_me|create_draft|wait_for_approval|automatically_reply|escalate",
  "reasoning": "2-4 sentences: what this email wants, which Step 4 rule you applied and why, and what made you uncertain."
}}
"""

DECISION_SUPPORT_PROMPT_TEMPLATE = """You are the safety check that runs before this system may answer an email on our behalf.

Assume the reply will be sent automatically, with nobody reading it first. Your one
job is to decide whether answering would require stating something we have not been
authorised to state.

Judge only against the Known Facts below. General knowledge (that Monday follows
Sunday) is fine. Anything specific to us - our prices, our availability, our
deadlines, our bank details, our opinion - must appear in the Known Facts, or it
counts as invented.

Set would_require_invented_facts to true if answering properly needs ANY of:
- a number, date, price or deadline not in the Known Facts
- a commitment, promise, approval or refusal we have not pre-authorised
- a personal or legal opinion
- confirming something we have no record of
When genuinely unsure, answer true. A false negative sends a wrong statement to a
real person; a false positive only asks a human to look.

=== EMAIL ===
Subject: {subject}

{body}

=== KNOWN FACTS ===
{known_facts}

=== OUTPUT ===
Reply with JSON only. No prose, no markdown fence.
{{
  "missing_information": ["each specific fact needed but absent"],
  "commitments_implied": ["each promise or obligation answering would create"],
  "would_require_invented_facts": false,
  "analysis_summary": "2-3 sentences naming the riskiest thing about replying automatically."
}}
"""

REPLY_GENERATION_PROMPT_TEMPLATE = """Write the reply to this email, as the person who owns this mailbox.

## Grounding - the rule that matters most
Every specific claim must come from the incoming email, the thread, or the Known
Facts. You may not supply a date, price, time, policy or commitment from your own
knowledge, however reasonable it seems. If a needed detail is missing, do not smooth
over it: write [NEEDS INPUT: what is missing] exactly where it belongs, add it to
placeholders_used, and set complete to false. A reply with an honest gap is useful;
a reply with an invented detail is worse than no reply at all.

## Voice
Write the way a competent person actually writes email: plain, warm, direct.
Match the sender's register - brief for a brief note, fuller for a careful one.
Answer every question they asked, in the order they asked it.
No filler ("I hope this email finds you well", "Please do not hesitate to reach out").
Do not restate their email back at them. Lead with the answer where you can.
Two to eight sentences unless the questions genuinely need more.
Sign off the way the thread already does. If the thread shows no sign-off and the
name is unknown, use [NEEDS INPUT: preferred sign-off name].

## Commitments
If the reply agrees to anything - a time, a deliverable, a price, a next step -
record it in commitments_made, in the words the recipient would hold us to.

=== EMAIL ===
From:    {sender_name} <{sender_email}>
Subject: {subject}

{body}

=== THREAD SO FAR ===
{thread_context}

=== KNOWN FACTS (the only facts you may state as ours) ===
{known_facts}

=== OUTPUT ===
Reply with JSON only. No prose, no markdown fence.
{{
  "reply_subject": "Re: ...",
  "reply_body": "the full email body, ready to send",
  "complete": true,
  "placeholders_used": [],
  "commitments_made": []
}}
"""


REPLY_REVIEW_PROMPT_TEMPLATE = """Check this draft reply before it is sent. You did not write it.

Another model wrote the reply below. Your job is to catch what it got wrong, not
to rewrite it or to praise it. Approve unless you can name something concrete.

Object if any of these is true:
* It states a fact that is not in the incoming email, the thread, or the Known
  Facts - a date, a price, a time, a policy, a name. An invented detail is the
  single worst failure here, so list every one you find in invented_details.
* It commits us to something the incoming email did not ask for, or agrees to
  more than was asked.
* It leaves one of the sender's questions unanswered.
* It would embarrass the owner: wrong tone for the thread, or careless about
  something the sender clearly cares about.

Do NOT object to: a [NEEDS INPUT: ...] placeholder (that is the correct way to
mark a genuine gap), brevity, or wording you would merely have phrased otherwise.

=== INCOMING EMAIL ===
From:    {sender_name} <{sender_email}>
Subject: {subject}

{body}

=== THREAD SO FAR ===
{thread_context}

=== KNOWN FACTS (the only facts we may state as ours) ===
{known_facts}

=== THE DRAFT REPLY TO CHECK ===
Subject: {reply_subject}

{reply_body}

=== OUTPUT ===
Reply with JSON only. No prose, no markdown fence.
{{
  "approved": true,
  "concerns": [],
  "invented_details": [],
  "confidence": 0.9
}}
"""


class AIProvider(ABC):
    # Subjects this person never wants filed away, in their own words. Set by
    # the factory from config.yaml. A tuple, not a list, so the shared default
    # cannot be appended to by accident.
    never_archive_about: tuple = ()

    def protected_topics_block(self) -> str:
        """The Step 3c block of the classification prompt.

        Whether an email matters is not something a model can work out from the
        email alone: a job-board status update looks like any other low
        importance notification, which is exactly how one got archived. This is
        the only place the person's own answer to that gets in.
        """
        topics = [t.strip() for t in (self.never_archive_about or ()) if t and t.strip()]
        if not topics:
            return (
                "This person has not named any. Leave protected_topic empty (\"\")."
            )
        listed = "\n".join(f'  - "{topic}"' for topic in topics)
        return (
            "They asked that mail about any of these is never filed away:\n"
            f"{listed}"
        )

    # The labels this person keeps, as (name, what belongs in it) pairs. Set by
    # the factory from config.yaml. A tuple for the same reason as above.
    labels: tuple = ()

    def labels_block(self) -> str:
        """The Step 3d block of the classification prompt."""
        kept = [
            (str(name).strip(), str(about or "").strip())
            for name, about in (self.labels or ())
            if str(name).strip()
        ]
        if not kept:
            return 'This person keeps no labels of their own. Leave suggested_label empty ("").'
        listed = "\n".join(
            f'  - "{name}"' + (f" - {about}" if about else "")
            for name, about in kept
        )
        return "Their labels, and what each one is for:\n" + listed

    def describe(self) -> tuple:
        """(provider, model) that would actually handle the next call.

        The audit log must name the provider that really answered, not the
        one named in config.yaml - after a failover those differ.
        """
        return ("unknown", "unknown")

    @abstractmethod
    def validate_setup(self) -> None:
        """Validate API key / runtime endpoint setup at startup. Raises ValueError if missing."""
        pass

    @abstractmethod
    def classify_email(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> EmailAnalysis:
        """Analyze and classify email into EmailAnalysis structured schema."""
        pass

    @abstractmethod
    def evaluate_decision_support(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> DecisionSupportOutput:
        """Evaluate decision support risk factors."""
        pass

    @abstractmethod
    def generate_reply(
        self,
        message: EmailMessage,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> ReplyGenerationOutput:
        """Generate grounded email reply."""
        pass

    def organise_facts(self, existing: str, addition: str) -> KnownFactsMerge:
        """Fold new information into the knowledge base and tidy it.

        Not abstract, and the default simply appends: a provider that cannot do
        this must never be the reason a fact goes missing. Losing a line here
        means the assistant stops knowing something true about a real person.
        """
        from email_workflow.core.known_facts import append_fact, facts_as_lines
        return KnownFactsMerge(
            facts=facts_as_lines(append_fact(existing, addition)),
            what_changed="added as written (no model available to tidy it)",
        )

    def suggest_facts(self, emails: str, existing: str = "") -> KnownFactsMerge:
        """Facts about the owner that their own mail states plainly.

        The default suggests nothing. A provider that cannot read the mail has
        nothing to offer here, and inventing something would be far worse than
        staying quiet.
        """
        return KnownFactsMerge(facts=[], what_changed="no model available")

    def review_reply(
        self,
        message: EmailMessage,
        reply: ReplyGenerationOutput,
        thread: Optional[ThreadState] = None,
        known_facts: str = "",
    ) -> ReplyReviewOutput:
        """A second opinion on a reply, meant to come from a different API key.

        Deliberately not abstract. A provider that cannot do this - the offline
        demo, a local runtime - simply raises no objection, so teamwork degrades
        to the single writer instead of breaking the run.
        """
        return ReplyReviewOutput(approved=True)

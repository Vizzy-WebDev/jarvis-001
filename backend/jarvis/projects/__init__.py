"""Talking an idea through until it is something someone can actually build.

The one structural decision: there is **no question queue**. The previous design
generated a list of questions and filed every subsequent reply against the next
unanswered slot — so a challenge ("why do I need accounts at all?") was recorded
as an answer rather than responded to, and the plan wrote itself the moment the
queue emptied. Removing the queue is the change that fixes that. What is on disk
instead is `decisions`: a plain, growing list of things actually settled, added
one at a time by an explicit tool call.
"""

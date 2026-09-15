"""HTTP routers, grouped the same way server/server.js groups its 147 routes.

Splitting by group is what makes the port checkable in waves: each module can be
held against its own slice of the recorded contract fixtures and signed off
independently, instead of the whole surface having to land before anything can
be verified.
"""

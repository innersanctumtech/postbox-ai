"""Delivery adapter for the Grok CLI's project-scoped inbox.

The adapter has no filesystem or process-discovery policy of its own. The
integrating application supplies those operations, which keeps this door
portable and straightforward to exercise with synthetic sessions.
"""


class GrokDoor:
    """Route mail to one live Grok session and store it in that project's inbox."""

    agent = "grok"

    def __init__(self, *, sessions_provider, same_project, inbox_writer, renderer):
        self._sessions_provider = sessions_provider
        self._same_project = same_project
        self._inbox_writer = inbox_writer
        self._renderer = renderer

    def resolve(self, project, exclude_pid=None, sessions=None):
        """Return at most one live session in the requested project.

        Session providers are ordered newest first. Grok has one inbox per
        project, so selecting multiple sessions would store duplicate mail.
        """
        sessions = self._sessions_provider() if sessions is None else sessions
        matches = [session for session in sessions
                   if self._same_project(session.project, project) and session.pid != exclude_pid]
        return matches[:1]

    def busy_note(self, session, now=None):
        """Explain that inbox delivery does not wake a running Grok session."""
        return (
            f"{session.label} cannot be woken: it reads this from its inbox "
            "(post inbox) when its next turn starts, so an answer can take a while"
        )

    def render(self, from_addr, text, waited_s=None):
        """Render a message with the instructions used by inbox-only delivery."""
        return self._renderer(from_addr, text, inbox=True, waited_s=waited_s)

    def deliver(self, session, text, timeout=None):
        """Store the message in the inbox for the selected project's Grok session."""
        self._inbox_writer(self.agent, session.project, text)

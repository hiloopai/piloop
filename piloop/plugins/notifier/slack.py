"""``slack``: a notifier that posts to a Slack channel through an incoming webhook."""

from collections.abc import Sequence

import httpx2 as httpx

from piloop.plugins.harness.contract import SecretRef
from piloop.plugins.notifier.contract import MessageRef, NotDelivered, Notice
from piloop.plugins.notifier.kit.slack import SlackText, refused
from piloop.plugins.secrets.contract import SecretReader


class Slack(SlackText):
    """``slack``: one message in the channel of an incoming webhook (https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks)."""

    webhook: SecretRef = SecretRef(secret='SLACK_WEBHOOK_URL')
    """The secret that holds the webhook's URL: ``SLACK_WEBHOOK_URL`` unless set (``piloop secret set``)."""

    def send(self, notices: Sequence[Notice], secrets: SecretReader) -> Sequence[MessageRef | None]:
        name = self.webhook.secret
        if (url := secrets.get(name)) is None:
            raise NotDelivered(secrets.missing(name))
        try:
            answer = httpx.post(
                url.get_secret_value(), json={'text': self.text(notices)}, timeout=self.timeout.total_seconds()
            )
        except httpx.InvalidURL as e:  # its message quotes a part of the URL, which is the secret
            raise NotDelivered(f'the secret {name} is not a URL to post to: set it to the webhook URL') from e
        except httpx.HTTPError as e:  # its message can hold the URL
            raise NotDelivered(f'{type(e).__name__}: {str(e).replace(url.get_secret_value(), name)}') from e
        if not answer.is_success:
            raise refused(answer)
        return [None] * len(notices)  # a webhook answers "ok", with no id of the post: it takes no replies

    def text(self, notices: Sequence[Notice]) -> str:
        """The message: one block for each of the first ``max_items`` notices, then how many more there are."""
        shown = [self.item(n) for n in notices[: self.max_items]]
        if more := len(notices) - len(shown):
            shown.append(f'… and {more} more: `piloop inbox`')
        return '\n\n'.join(shown)

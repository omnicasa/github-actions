#!/usr/bin/env bash
set -euo pipefail

if [ -z "${WEBHOOK_URL:-}" ]; then
  echo "Slack webhook-url is blank, skipping notification"
  exit 0
fi

case "$STATUS" in
  success) color="#2eb886" ;;
  failure) color="#d93f0b" ;;
  *) color="#dbab09" ;;
esac

# "Key: Value" lines -> one mrkdwn line each, in a single section. Slack's `fields`
# would lay the same pairs out two-up, which reorders them across columns and wraps
# badly on a phone; one line per fact keeps the given order. A line without a colon
# still renders rather than aborting the whole notification. Section text is capped at
# 3000 characters by Slack, so a runaway fact truncates instead of losing the message.
facts_text=""
if [ -n "${FACTS:-}" ]; then
  facts_text=$(printf '%s\n' "$FACTS" | jq -Rrn '
    def mrkdwn: gsub("&"; "&amp;") | gsub("<"; "&lt;") | gsub(">"; "&gt;");
    [ inputs
      | select(. != "")
      | capture("^(?<title>[^:]+):\\s*(?<value>.*)$")? // {title: ., value: ""}
      | "*\(.title|mrkdwn):* \(.value|mrkdwn)"
    ] | join("\n") | .[0:2900]
  ')
fi

body_json=$(jq -n \
  --arg title "$TITLE" \
  --arg color "$color" \
  --arg facts "$facts_text" \
  --arg runUrl "${RUN_URL:-}" \
  '
  def mrkdwn: gsub("&"; "&amp;") | gsub("<"; "&lt;") | gsub(">"; "&gt;");
  ($title | mrkdwn) as $t |
  {
    attachments: [{
      color: $color,
      # Notification and sidebar preview only — a top-level `text` would render the
      # headline a second time above the card.
      fallback: $title,
      blocks: (
        # Slack link syntax is <url|text>, not markdown. The headline carries the link,
        # which is why there is no separate "Open run" button.
        [{type: "section", text: {type: "mrkdwn", text: (if $runUrl != "" then "*<\($runUrl)|\($t)>*" else "*\($t)*" end)}}]
        + (if $facts != "" then [{type: "section", text: {type: "mrkdwn", text: $facts}}] else [] end)
      )
    }]
  }')

# Set only by CI, to assert the payload builds and is well-formed without a webhook.
if [ "${NOTIFY_DRY_RUN:-}" = "1" ]; then
  printf '%s\n' "$body_json"
  exit 0
fi

curl -sSf -X POST -H "Content-Type: application/json" -d "$body_json" "$WEBHOOK_URL" -o /dev/null

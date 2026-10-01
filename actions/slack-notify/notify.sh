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

# One line per fact, not Slack `fields`: those lay out two-up, reordering the facts
# across columns. Truncated because Slack caps section text at 3000 characters.
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
      # Preview text. A top-level `text` renders the headline a second time above the card.
      fallback: $title,
      blocks: (
        [{type: "section", text: {type: "mrkdwn", text: (if $runUrl != "" then "*<\($runUrl)|\($t)>*" else "*\($t)*" end)}}]
        + (if $facts != "" then [{type: "section", text: {type: "mrkdwn", text: $facts}}] else [] end)
      )
    }]
  }')

# Set by CI only, to assert the payload builds without a webhook.
if [ "${NOTIFY_DRY_RUN:-}" = "1" ]; then
  printf '%s\n' "$body_json"
  exit 0
fi

curl -sSf -X POST -H "Content-Type: application/json" -d "$body_json" "$WEBHOOK_URL" -o /dev/null
